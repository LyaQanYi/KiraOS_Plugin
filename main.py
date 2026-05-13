"""
KiraOS Plugin — Combines two OS-level capabilities:

  1. **Dual-Brain Memory (TOML + SQLite)**: Two-tier memory engine. Fast loop
     (FTS5 retrieval with jieba CJK tokenizer + optional sqlite-vec embeddings)
     serves the LLM in real time; slow loop (hippocampus) runs as a background
     asyncio task after every few turns to extract facts, deduplicate, generate
     reflections, and update profiles. TOML files are the source of truth,
     SQLite is a rebuildable index.

     Tools: memory_add, memory_search, memory_update_entry, memory_remove,
            profile_view, profile_update.
     Hook:  inject distilled profile + recalled memories into the system
            prompt; feed each user/assistant chunk into the hippocampus buffer.

  2. **Skill Router (Progressive Disclosure)**: scans `data/skills/` for skill
     folders, each in either:
        - SKILL.md (YAML frontmatter + body, Claude-Skills-style), or
        - manifest.json + instruction.md (legacy two-file format).
     When the LLM triggers a skill, the body is loaded and returned as the
     tool result so the main LLM follows the instructions in the SAME tool-loop
     turn — zero extra LLM API calls. A skill folder may also bundle resource
     files under `references/`, `resources/`, `scripts/`, or `data/`; the LLM
     pulls these on demand via the `read_skill_resource` tool.
"""

import asyncio
from typing import Optional

from core.plugin import BasePlugin, logger, on, Priority, register_tool
from core.provider import LLMRequest
from core.prompt_manager import Prompt
from core.chat.message_utils import KiraMessageBatchEvent, KiraMessageEvent, KiraStepResult
from core.utils.path_utils import get_data_path

from .memory import MemoryManager
from .memory.memory_paths import (
    set_data_root,
    ENTITY_USER,
    ENTITY_GROUP,
)
from .memory.migrations import migrate_legacy_db_if_needed
from . import tools as memory_tools
from .skill_router import SkillRouter, SkillInfo

# ════════════════════════════════════════════════════════════════════
#  Prompt Fragments
# ════════════════════════════════════════════════════════════════════

SKILL_FEW_SHOT_HEADER = "技能工具（调用后按返回的指令执行）: "


# ════════════════════════════════════════════════════════════════════
#  LLM Client Adapter
# ════════════════════════════════════════════════════════════════════
#
# 内部的 MemoryExtractor 用 messages-list 风格调用 LLM：
#   await client.chat([{"role": "user", "content": ...}]) -> obj.text_response
# 而 KiraAI 的 LLMModelClient 的实际签名是
#   await client.chat(LLMRequest) -> LLMResponse (带 .text_response)
# 这个小 adapter 把两套对接起来。多支持一个 .chat_fast() 让海马体可以用 fast
# LLM 跑提取（成本更低、延迟更小），不存在则回退到 default。

class _MemoryLLMAdapter:
    """把 messages-list 风格的 chat 调用适配到 KiraAI 的 chat(LLMRequest)。"""

    def __init__(self, default_client, fast_client=None):
        self._default = default_client
        self._fast = fast_client or default_client

    async def chat(self, messages: list):
        req = LLMRequest(messages=list(messages))
        return await self._default.chat(req)

    async def chat_fast(self, messages: list):
        req = LLMRequest(messages=list(messages))
        return await self._fast.chat(req)


MEMORY_ACTIVE_RECALL_HINT = (
    "📝 本轮检查：如果用户提到任何关于自身的事实信息（姓名/地点/职业/关系/偏好/经历），"
    "主动调用 memory_add 记录；如果需要回忆过往，调用 memory_search。"
    "宁记错不漏过——低置信度会被后续高置信度自动合并。"
)


# ════════════════════════════════════════════════════════════════════
#  Plugin Class
# ════════════════════════════════════════════════════════════════════

class UserMemoryPlugin(BasePlugin):
    """KiraOS Plugin — Dual-Brain Memory + Skill Router."""

    BUILTIN_MEMORY_PLUGIN_ID = "kira_plugin_simple_memory"

    def __init__(self, ctx, cfg: dict):
        super().__init__(ctx, cfg)

        # ── Memory config ───────────────────────────────────────────
        self._data_root = get_data_path() / "memory"
        self._hippocampus_threshold = int(cfg.get("hippocampus_threshold", 3))
        self._recall_top_k = int(cfg.get("recall_top_k", 5))
        self._max_memory_length = int(cfg.get("max_memory_length", 20))
        # 海马体每条 LLM 调用的超时（秒）；卡慢的 provider 调大、本地快模型可以调小。
        # schema 里声明的是 integer，min=5 / max=300——这里也走 int() 保持类型
        # 一致（"多少秒"的语义本就是整数）。
        #
        # **Clamp 到 schema 的 [5, 300] 区间**：WebUI 会做校验，但用户手编
        # config 文件或直接调 API 写入仍可注入 0 / 负数 / 极大值。0 或负数会
        # 让 `asyncio.wait_for` 立即超时把海马体饿死；过大值（小时级）会让一
        # 次卡死的 LLM 请求长时间占住背景 task。代码层 clamp 是最后一道闸。
        try:
            raw = int(cfg.get("llm_chat_timeout", 30))
        except (TypeError, ValueError):
            raw = 30
        self._llm_chat_timeout = max(5, min(300, raw))
        self._auto_migrate = bool(cfg.get("auto_migrate_legacy_db", True))
        self._enable_decay = bool(cfg.get("enable_decay", True))

        # Which sections of the profile/memory to inject into system prompt
        self._inject_profile = bool(cfg.get("inject_profile", True))
        self._inject_facts = bool(cfg.get("inject_facts", True))
        self._inject_reflections = bool(cfg.get("inject_reflections", True))

        # The handle to the dual-brain manager. Set in initialize().
        self.memory_manager: Optional[MemoryManager] = None

        # Per-event dedup for hippocampus feed (a single batch may fire
        # step_result multiple times in multi-step tool loops).
        self._fed_event_ids: set[int] = set()
        self._max_fed_dedup_size = 10_000

        # ── Skill Router config ─────────────────────────────────────
        skills_dir = cfg.get("skills_dir", "") or str(get_data_path() / "skills")
        self.skill_router = SkillRouter(skills_dir)
        self._registered_skill_names: list[str] = []
        self._disabled_skills: set = set(cfg.get("disabled_skills", []))
        self._command_map: dict[str, SkillInfo] = {}
        self._enable_slash_commands: bool = bool(cfg.get("enable_slash_commands", False))
        self._resource_tool_registered: bool = False

        # ── WebUI config ────────────────────────────────────────────
        self._webui_port = int(cfg.get("webui_port", 0))
        self._webui_host = str(cfg.get("webui_host", "127.0.0.1"))
        self._webui_token = str(cfg.get("webui_token", ""))
        self._webui_server: object | None = None

        # Tools registered dynamically via ctx.llm_api (not via @register_tool)
        # so we can pass them the memory_manager handle through closure.
        self._registered_memory_tool_names: list[str] = []

    # ════════════════════════════════════════════════════════════════
    #  Lifecycle
    # ════════════════════════════════════════════════════════════════

    async def initialize(self):
        await self._disable_builtin_memory()

        # ── Memory: data root + legacy migration + manager ─────────
        set_data_root(self._data_root)

        if self._auto_migrate:
            legacy_db_path = self._data_root / "kiraos.db"
            try:
                migrated = await migrate_legacy_db_if_needed(
                    legacy_db_path, self._data_root
                )
                if migrated:
                    logger.info("Legacy KiraOS memory migrated to v3 TOML structure")
            except Exception as e:
                logger.error(f"Legacy memory migration failed: {e}", exc_info=True)

        self.memory_manager = MemoryManager(
            max_memory_length=self._max_memory_length,
            hippocampus_threshold=self._hippocampus_threshold,
            llm_chat_timeout=self._llm_chat_timeout,
        )
        await self.memory_manager.async_init()

        # Wire in the host LLM as the hippocampus LLM client. The extractor
        # expects `await client.chat(messages_list)` returning `.text_response`
        # — KiraAI's LLMModelClient uses `.chat(LLMRequest)`, so we adapt.
        try:
            default_llm = self.ctx.get_default_llm_client()
        except Exception as e:
            default_llm = None
            logger.warning(f"Could not resolve default LLM client: {e}")
        try:
            fast_llm = self.ctx.get_default_fast_llm_client()
        except Exception:
            fast_llm = None

        if default_llm is not None:
            adapter = _MemoryLLMAdapter(default_llm, fast_llm)
            self.memory_manager.set_llm_client(adapter)
            logger.info(
                f"Hippocampus LLM client wired (default={getattr(default_llm.model, 'model_id', '?')}, "
                f"fast={getattr(fast_llm.model, 'model_id', '?') if fast_llm else 'same'})"
            )
        else:
            logger.warning(
                "No default LLM client available — hippocampus will skip fact extraction "
                "(memory_add/search/profile_* tools still work via TOML+FTS5)"
            )

        logger.info("Dual-brain memory ready")

        # ── Register the 6 memory tools dynamically ────────────────
        self._register_memory_tools()

        # ── Discover & register skills ──────────────────────────────
        skills = self.skill_router.discover()
        any_with_resources = False
        for skill in skills:
            if skill.name in self._disabled_skills:
                logger.info(f"Skill '{skill.name}' is disabled, skipping registration")
                continue
            self._register_skill_tool(skill)
            if skill.has_resources():
                any_with_resources = True

        self._command_map = self.skill_router.get_commands(enabled_only=set(self._registered_skill_names))

        if skills:
            active = len(self._registered_skill_names)
            logger.info(f"Registered {active}/{len(skills)} skill(s): {self._registered_skill_names}")
        else:
            logger.info("No skills found (place skill folders in data/skills/)")

        if any_with_resources:
            self._register_resource_tool()

        # ── Start Memory WebUI ──────────────────────────────────────
        if self._webui_port > 0 and self.memory_manager:
            from .web_server import WebUIServer
            self._webui_server = WebUIServer(
                memory_manager=self.memory_manager,
                host=self._webui_host,
                port=self._webui_port,
                token=self._webui_token,
            )
            await self._webui_server.start()

        logger.info("KiraOS plugin initialized (dual-brain memory + skill router)")

    async def _disable_builtin_memory(self):
        """Auto-detect and disable the builtin Simple Memory plugin to prevent conflicts."""
        try:
            mgr = self.ctx.plugin_mgr
            if mgr is None:
                return
            if mgr.is_plugin_enabled(self.BUILTIN_MEMORY_PLUGIN_ID):
                await mgr.set_plugin_enabled(self.BUILTIN_MEMORY_PLUGIN_ID, False)
                logger.warning(
                    "检测到内置记忆插件(Simple Memory)已启用，已自动禁用以避免冲突。"
                    "如需切换回内置记忆，请在 WebUI 禁用 KiraOS 后重新启用 Simple Memory。"
                )
        except Exception as e:
            logger.warning(f"检查内置记忆插件状态时出错: {e}")

    async def terminate(self):
        if self._webui_server:
            try:
                await self._webui_server.stop()
            except Exception as e:
                logger.warning(f"Failed to stop WebUI server: {e}")
            self._webui_server = None

        # Drain in-flight hippocampus tasks so TOML writes don't get
        # cut off mid-flight during shutdown. 30s ceiling — generous
        # because hippocampus involves LLM calls (extraction + reflection).
        if self.memory_manager and self.memory_manager._background_tasks:
            pending = list(self.memory_manager._background_tasks)
            try:
                await asyncio.wait_for(
                    asyncio.gather(*pending, return_exceptions=True),
                    timeout=30.0,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    f"[hippocampus] {len(pending)} background task(s) still pending "
                    "at shutdown; cancelling"
                )
                for t in pending:
                    if not t.done():
                        t.cancel()
                try:
                    await asyncio.wait_for(
                        asyncio.gather(*pending, return_exceptions=True),
                        timeout=2.0,
                    )
                except asyncio.TimeoutError:
                    logger.warning("[hippocampus] some tasks did not exit after cancel()")

        # Unregister memory tools
        for name in self._registered_memory_tool_names:
            try:
                self.ctx.llm_api.unregister_tool(name)
            except Exception as e:
                logger.warning(f"Failed to unregister tool '{name}': {e}")
        self._registered_memory_tool_names.clear()

        # Unregister skill tools
        for name in self._registered_skill_names:
            try:
                self.ctx.llm_api.unregister_tool(name)
            except Exception as e:
                logger.warning(f"Failed to unregister tool '{name}': {e}")
        self._registered_skill_names.clear()

        if self._resource_tool_registered:
            try:
                self.ctx.llm_api.unregister_tool("read_skill_resource")
            except Exception as e:
                logger.warning(f"Failed to unregister read_skill_resource: {e}")
            self._resource_tool_registered = False

        self.memory_manager = None
        logger.info("KiraOS plugin terminated")

    # ════════════════════════════════════════════════════════════════
    #  Memory — Tool registration
    # ════════════════════════════════════════════════════════════════

    def _register_memory_tools(self):
        """Register the 6 dual-brain memory tools as LLM tools.

        Each tool's executor closes over ``self`` so it can reach the
        memory_manager handle bound in ``initialize()``.
        """
        plugin = self

        async def _add(event, *_, **kwargs) -> str:
            return await memory_tools.memory_add(plugin.memory_manager, event, **kwargs)

        async def _search(event, *_, **kwargs) -> str:
            return await memory_tools.memory_search(plugin.memory_manager, event, **kwargs)

        async def _update(event, *_, **kwargs) -> str:
            return await memory_tools.memory_update_entry(plugin.memory_manager, event, **kwargs)

        async def _remove(event, *_, **kwargs) -> str:
            return await memory_tools.memory_remove(plugin.memory_manager, event, **kwargs)

        async def _profile_view(event, *_, **kwargs) -> str:
            return await memory_tools.profile_view(plugin.memory_manager, event, **kwargs)

        async def _profile_update(event, *_, **kwargs) -> str:
            return await memory_tools.profile_update(plugin.memory_manager, event, **kwargs)

        bindings = [
            ("memory_add", _add),
            ("memory_search", _search),
            ("memory_update_entry", _update),
            ("memory_remove", _remove),
            ("profile_view", _profile_view),
            ("profile_update", _profile_update),
        ]
        for tool_name, executor in bindings:
            schema = memory_tools.TOOL_SCHEMAS[tool_name]
            self.ctx.llm_api.register_tool(
                name=tool_name,
                description=schema["description"],
                parameters=schema["params"],
                func=executor,
            )
            self._registered_memory_tool_names.append(tool_name)

        logger.info(f"Registered memory tools: {self._registered_memory_tool_names}")

    # ════════════════════════════════════════════════════════════════
    #  Skill Router
    # ════════════════════════════════════════════════════════════════

    def _register_skill_tool(self, skill: SkillInfo):
        async def _skill_executor(event: KiraMessageBatchEvent, *_, **kwargs) -> str:
            return self._execute_skill(skill, event, **kwargs)

        self.ctx.llm_api.register_tool(
            name=skill.name,
            description=skill.tool_description,
            parameters=skill.parameters,
            func=_skill_executor,
        )
        self._registered_skill_names.append(skill.name)

    def _execute_skill(self, skill: SkillInfo, event: KiraMessageBatchEvent, **kwargs) -> str:
        """Load instruction (with substitution + exclude guard) and return as tool_result."""
        logger.info(f"Loading skill '{skill.name}' instruction (args: {kwargs})")

        instruction = self.skill_router.build_instruction_prompt(skill, kwargs)
        if not instruction:
            return f"Error: skill '{skill.name}' has empty instruction"

        parts = []
        parts.append(f"<skill name=\"{skill.name}\">")
        parts.append(instruction)

        if skill.has_resources():
            resources = self.skill_router.list_resources(skill)
            if resources:
                listed = "\n".join(f"  - {r}" for r in resources[:30])
                more = f"\n  ... (+{len(resources) - 30} more)" if len(resources) > 30 else ""
                parts.append(
                    f"\n<resources>\n该技能附带以下资源文件，需要时调用 "
                    f"read_skill_resource(skill_name=\"{skill.name}\", path=\"...\") 读取：\n"
                    f"{listed}{more}\n</resources>"
                )

        parts.append("</skill>")
        parts.append("请严格按照上述技能指令执行，直接输出执行结果。")

        return "\n".join(parts)

    def _register_resource_tool(self):
        """Register the third-tier read_skill_resource tool."""
        async def _read_resource(event: KiraMessageBatchEvent, *_,
                                 skill_name: str = "", path: str = "", **__) -> str:
            if not skill_name or not path:
                return "Error: both 'skill_name' and 'path' are required"
            skill = self.skill_router.get_skill(skill_name)
            if not skill:
                return f"Error: skill '{skill_name}' not found"
            if skill.name not in self._registered_skill_names:
                return f"Error: skill '{skill_name}' is disabled"
            ok, content = self.skill_router.read_resource(skill, path)
            if not ok:
                return content
            return (
                f"<resource skill=\"{skill.name}\" path=\"{path}\">\n"
                f"{content}\n</resource>"
            )

        self.ctx.llm_api.register_tool(
            name="read_skill_resource",
            description=(
                "读取技能附带的资源文件（如 references/、resources/、scripts/、data/ "
                "目录下的文件）。仅当某技能的指令明确要求查阅其资源文件时才调用。"
                "参数: skill_name=技能名, path=相对技能根目录的路径（如 'references/spec.md'）。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "skill_name": {"type": "string", "description": "技能名称"},
                    "path": {"type": "string", "description": "相对技能根目录的资源文件路径"},
                },
                "required": ["skill_name", "path"],
            },
            func=_read_resource,
        )
        self._resource_tool_registered = True
        logger.info("Registered read_skill_resource tool (third-tier disclosure)")

    async def _reload_skills(self):
        """Hot reload: unregister old skills, rediscover, re-register."""
        for name in self._registered_skill_names:
            try:
                self.ctx.llm_api.unregister_tool(name)
            except Exception as e:
                logger.warning(f"Failed to unregister tool '{name}': {e}")
        self._registered_skill_names.clear()

        skills = self.skill_router.reload()
        any_with_resources = False
        for skill in skills:
            if skill.name not in self._disabled_skills:
                self._register_skill_tool(skill)
                if skill.has_resources():
                    any_with_resources = True
        self._command_map = self.skill_router.get_commands(enabled_only=set(self._registered_skill_names))

        if any_with_resources and not self._resource_tool_registered:
            self._register_resource_tool()
        elif not any_with_resources and self._resource_tool_registered:
            try:
                self.ctx.llm_api.unregister_tool("read_skill_resource")
            except Exception as e:
                logger.warning(f"Failed to unregister read_skill_resource: {e}")
            self._resource_tool_registered = False

        logger.info(f"Reloaded skills: {len(self._registered_skill_names)} active")

    # ════════════════════════════════════════════════════════════════
    #  Slash Command Interception
    # ════════════════════════════════════════════════════════════════

    @on.im_message(priority=Priority.HIGH)
    async def handle_slash_command(self, event: KiraMessageEvent):
        """Intercept /command messages and nudge LLM to call the matching skill."""
        if not self._enable_slash_commands:
            return
        from core.chat.message_elements import Text

        text = ""
        for elem in event.message.chain:
            if isinstance(elem, Text):
                text += elem.text
        text = text.strip()
        if not text.startswith("/"):
            return

        parts = text.split(None, 1)
        cmd = parts[0]
        args_text = parts[1] if len(parts) > 1 else ""

        skill = self._command_map.get(cmd)
        if not skill:
            return

        non_text = [e for e in event.message.chain if not isinstance(e, Text)]
        event.message.chain = [Text(f"[用户使用了技能命令 {cmd}] {args_text}")] + non_text
        logger.info(f"Slash command '{cmd}' matched skill '{skill.name}'")

    # ════════════════════════════════════════════════════════════════
    #  Helpers
    # ════════════════════════════════════════════════════════════════

    @staticmethod
    def _extract_user_ids(event: KiraMessageBatchEvent) -> list[str]:
        seen = set()
        user_ids = []
        for msg in event.messages:
            if msg.sender and msg.sender.user_id:
                uid = msg.sender.user_id
                if uid not in seen and uid != "unknown":
                    seen.add(uid)
                    user_ids.append(uid)
        return user_ids

    @staticmethod
    def _get_primary_user_id(event: KiraMessageBatchEvent) -> str:
        if event.messages:
            last_msg = event.messages[-1]
            if last_msg.sender and last_msg.sender.user_id:
                return last_msg.sender.user_id
        return "unknown"

    @staticmethod
    def _extract_latest_user_text(event: KiraMessageBatchEvent) -> str:
        """Concatenate the text of the most recent user message."""
        try:
            from core.chat.message_elements import Text
        except ImportError:
            Text = None

        if not event.messages:
            return ""
        last = event.messages[-1]
        parts = []
        try:
            chain = last.message.chain
        except AttributeError:
            return ""
        for elem in chain:
            if Text is not None and isinstance(elem, Text):
                parts.append(elem.text)
            elif hasattr(elem, "text"):
                parts.append(getattr(elem, "text", ""))
        return " ".join(p for p in parts if p).strip()

    def _build_session_id(self, event: KiraMessageBatchEvent) -> str:
        """Construct an `adapter:type:id` session string for the hippocampus.

        Falls back to `unknown:dm:<uid>` if event lacks the needed fields.
        """
        adapter = ""
        try:
            adapter = event.adapter.name if event.adapter else ""
        except AttributeError:
            adapter = ""

        sess = getattr(event, "session", None)
        if sess is not None:
            session_type = getattr(sess, "session_type", "") or "dm"
            session_id = getattr(sess, "session_id", "")
            if session_id:
                return f"{adapter or 'unknown'}:{session_type}:{session_id}"

        uid = self._get_primary_user_id(event)
        return f"{adapter or 'unknown'}:dm:{uid}"

    # ════════════════════════════════════════════════════════════════
    #  LLM Hook — inject memory context + skill manifest descriptions
    # ════════════════════════════════════════════════════════════════

    @on.llm_request()
    async def inject_context(self, event: KiraMessageBatchEvent, req: LLMRequest, *_):
        """Inject distilled profile + recalled long-term memory before each LLM call."""
        if not self.memory_manager:
            return

        memory_blocks = []

        # ── 1. Per-user profile prompts ─────────────────────────────
        if self._inject_profile:
            for uid in self._extract_user_ids(event):
                try:
                    prompt = await self.memory_manager.get_profile_prompt(
                        uid, ENTITY_USER
                    )
                    if prompt and prompt.strip() and prompt.strip() != "暂无画像信息":
                        memory_blocks.append(f"[用户 {uid} 画像]\n{prompt}")
                except Exception as e:
                    logger.warning(f"Failed to load profile for {uid}: {e}")

        # ── 2. Top-K recalled memories for the latest query ─────────
        if self._inject_facts or self._inject_reflections:
            query = self._extract_latest_user_text(event)
            if query:
                primary_uid = self._get_primary_user_id(event)
                if primary_uid and primary_uid != "unknown":
                    try:
                        memories = await self.memory_manager.recall(
                            query=query,
                            entity_id=primary_uid,
                            entity_type=ENTITY_USER,
                            k=self._recall_top_k,
                        )
                        recalled_lines = []
                        for mem in memories or []:
                            if mem.type == "reflection" and not self._inject_reflections:
                                continue
                            if mem.type == "fact" and not self._inject_facts:
                                continue
                            tags_str = f" [{', '.join(mem.tags)}]" if mem.tags else ""
                            recalled_lines.append(f"- [{mem.type}]{tags_str} {mem.raw_text}")
                        if recalled_lines:
                            memory_blocks.append("[相关记忆]\n" + "\n".join(recalled_lines))
                    except Exception as e:
                        logger.warning(f"Recall failed: {e}")

        memory_context = "\n\n".join(memory_blocks)

        # ── 3. Skill manifest one-liner ─────────────────────────────
        skill_line = ""
        if self._registered_skill_names:
            names = []
            for sn in self._registered_skill_names:
                sk = self.skill_router.get_skill(sn)
                if sk:
                    names.append(sk.name)
            if names:
                skill_line = SKILL_FEW_SHOT_HEADER + ", ".join(names) + "\n"

        # ── 4. Splice into existing system prompt ───────────────────
        injected_memory = False
        for p in req.system_prompt:
            if p.name == "memory" and memory_context:
                p.content += f"\n{memory_context}"
                injected_memory = True
            if p.name == "tools" and skill_line:
                p.content += skill_line

        if not injected_memory and memory_context:
            req.system_prompt.append(
                Prompt(memory_context, name="memory", source="kiraos")
            )

        req.system_prompt.append(Prompt(
            MEMORY_ACTIVE_RECALL_HINT,
            name="memory_hint",
            source="kiraos",
        ))

    # ════════════════════════════════════════════════════════════════
    #  Hippocampus Feed — pass each user/assistant turn into the
    #  background hippocampus buffer. Threshold-based auto-trigger.
    # ════════════════════════════════════════════════════════════════

    @on.step_result()
    async def feed_hippocampus(
        self,
        event: KiraMessageBatchEvent,
        step_result: KiraStepResult,
    ):
        """Push the just-completed user/assistant chunk into the hippocampus.

        Per-event dedup: a single ``KiraMessageBatchEvent`` may produce multiple
        step_results during multi-step tool loops; we only feed once per batch.
        """
        if not self.memory_manager:
            return
        eid = id(event)
        if eid in self._fed_event_ids:
            return
        self._fed_event_ids.add(eid)
        if len(self._fed_event_ids) > self._max_fed_dedup_size:
            # Best-effort cap — id() values can repeat after GC anyway.
            self._fed_event_ids.clear()

        user_text = self._extract_latest_user_text(event)
        assistant_text = getattr(step_result, "raw_output", "") or ""
        if not user_text and not assistant_text:
            return

        primary_uid = self._get_primary_user_id(event)
        sender_name = ""
        try:
            if event.messages:
                last = event.messages[-1]
                if last.sender:
                    sender_name = last.sender.nickname or ""
        except AttributeError:
            pass

        chunk = []
        if user_text:
            chunk.append({
                "role": "user",
                "content": user_text,
                "sender_id": primary_uid if primary_uid != "unknown" else "",
                "sender_name": sender_name,
            })
        if assistant_text:
            chunk.append({
                "role": "assistant",
                "content": assistant_text,
            })

        if not chunk:
            return

        session_id = self._build_session_id(event)
        try:
            await self.memory_manager.update_memory(session_id, chunk)
        except Exception as e:
            logger.warning(f"Hippocampus feed failed for {session_id}: {e}")
