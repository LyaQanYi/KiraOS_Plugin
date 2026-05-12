"""KiraOS Plugin — combines:

  1. **Memory subsystem** (lightning-style)
       TOML file tree + SQLite/FTS5 index + optional sqlite-vec.
       Hippocampus runs asynchronously after every assistant turn,
       extracting facts, dedupe-merging, elevating into reflections, and
       updating each entity's profile.
       Six LLM tools: ``memory_add / memory_update / memory_remove /
       memory_search / profile_view / profile_update``.

  2. **Skill Router (progressive disclosure)** — unchanged from prior
       revision. Scans ``data/skills/`` for ``SKILL.md`` (or legacy
       ``manifest.json + instruction.md``) folders; when the LLM triggers a
       skill the body is returned as the tool result so the main loop
       follows it in the same turn.
"""

import asyncio
from typing import Optional

from core.plugin import BasePlugin, logger, on, Priority, register_tool
from core.provider import LLMRequest
from core.prompt_manager import Prompt
from core.chat.message_utils import (
    KiraMessageBatchEvent,
    KiraMessageEvent,
    KiraStepResult,
)
from core.utils.path_utils import get_data_path

from .memory.memory_manager import MemoryManager
from .memory.memory_index import MemoryIndex
from .memory.toml_tree_store import TomlTreeStore
from .memory.entity_profile import EntityProfileStore
from .memory.memory_extractor import MemoryExtractor
from .memory.memory_decay import MemoryDecayEngine
from .memory.memory_paths import ensure_directory_structure
from .skill_router import SkillRouter, SkillInfo
from . import migrate as legacy_migrate
from .tools import memory_tools


SKILL_FEW_SHOT_HEADER = "技能工具（调用后按返回的指令执行）: "


def _adapter_of(event) -> str:
    try:
        return event.adapter.name
    except AttributeError:
        return "unknown"


def _primary_speaker(event: KiraMessageBatchEvent) -> tuple[str, str, str]:
    """Return ``(adapter, sender_id, sender_nickname)`` of the most recent
    user message; falls back to empty strings when unavailable."""
    if not getattr(event, "messages", None):
        return "unknown", "", ""
    adapter = _adapter_of(event)
    for msg in reversed(event.messages):
        sender = getattr(msg, "sender", None)
        sid = getattr(sender, "user_id", "") or ""
        if sid and sid != "unknown":
            nick = getattr(sender, "nickname", "") or ""
            return adapter, sid, nick
    return adapter, "", ""


def _conversation_text_from_event(
    event: KiraMessageBatchEvent, assistant_reply: str = ""
) -> str:
    """Render the message batch into the ``昵称(ID): 内容`` format that the
    extractor prompts expect; the assistant's just-now reply is appended as
    ``Bot:`` so the hippocampus sees both sides of the exchange."""
    try:
        from core.chat.message_elements import Text
    except ImportError:
        return ""

    lines = []
    for msg in event.messages:
        sender = getattr(msg, "sender", None)
        sender_id = getattr(sender, "user_id", "") or ""
        sender_name = getattr(sender, "nickname", "") or sender_id or "User"
        chain = getattr(getattr(msg, "message", None), "chain", None)
        if not chain:
            continue
        text = "".join(
            elem.text for elem in chain if isinstance(elem, Text)
        ).strip()
        if not text:
            continue
        label = f"{sender_name}({sender_id})" if sender_id else sender_name
        lines.append(f"{label}: {text}")

    if assistant_reply:
        lines.append(f"Bot: {assistant_reply.strip()}")
    return "\n".join(lines)


class UserMemoryPlugin(BasePlugin):
    """KiraOS plugin entry point."""

    BUILTIN_MEMORY_PLUGIN_ID = "kira_plugin_simple_memory"

    def __init__(self, ctx, cfg: dict):
        super().__init__(ctx, cfg)

        # ── Memory subsystem config ─────────────────────────────────────
        self._memory_top_k = int(cfg.get("memory_top_k", 5))
        self._memory_inject_max_chars = int(cfg.get("memory_inject_max_chars", 800))
        self._enable_vector_search = bool(cfg.get("enable_vector_search", False))

        # Legacy db lives at <data>/memory/kiraos.db; we look for it once at
        # init time and run the migrator if present.
        data_dir = get_data_path()
        self._memory_dir = data_dir / "memory"
        self._legacy_db_path = str(self._memory_dir / "kiraos.db")
        self._memory_index_db_path = str(self._memory_dir / "memory_index.db")

        # Decay / GC config (parsed into the engine after init)
        self._decay_enabled = bool(cfg.get("decay_enabled", True))
        self._decay_interval_days = int(cfg.get("decay_interval_days", 14))
        self._gc_importance_threshold = int(cfg.get("gc_importance_threshold", 3))
        self._gc_unaccessed_days = int(cfg.get("gc_unaccessed_days", 30))

        # Hippocampus config
        self._hippocampus_enabled = bool(cfg.get("hippocampus_enabled", True))
        self._hippocampus_model_uuid = str(cfg.get("hippocampus_model_uuid", "") or "")
        raw_skip = cfg.get("hippocampus_skip_keywords")
        if raw_skip is None:
            raw_skip = [
                "别记", "忘了它", "随便说说", "随便聊",
                "开玩笑", "假设说", "假如", "假设",
            ]
        if isinstance(raw_skip, str):
            raw_skip = [s.strip() for s in raw_skip.split(",") if s.strip()]
        self._hippocampus_skip_keywords: list[str] = (
            list(raw_skip) if isinstance(raw_skip, list) else []
        )
        try:
            raw_inflight = int(cfg.get("hippocampus_max_inflight", 4))
        except (TypeError, ValueError):
            raw_inflight = 4
        self._hippocampus_max_inflight = max(1, min(32, raw_inflight))
        self._hippocampus_semaphore: Optional[asyncio.Semaphore] = None
        self._hippocampus_tasks: set[asyncio.Task] = set()
        self._processed_event_ids: set[int] = set()
        self._max_processed_dedup_size = 10_000

        # ── Memory subsystem state ──────────────────────────────────────
        self.memory_manager: Optional[MemoryManager] = None

        # ── Skill Router config ─────────────────────────────────────────
        skills_dir = cfg.get("skills_dir", "") or str(data_dir / "skills")
        self.skill_router = SkillRouter(skills_dir)
        self._registered_skill_names: list[str] = []
        self._disabled_skills: set[str] = set(cfg.get("disabled_skills", []))
        self._command_map: dict[str, SkillInfo] = {}
        self._enable_slash_commands: bool = bool(cfg.get("enable_slash_commands", False))
        self._resource_tool_registered: bool = False

        # ── WebUI config ────────────────────────────────────────────────
        self._webui_port = int(cfg.get("webui_port", 0))
        self._webui_host = str(cfg.get("webui_host", "127.0.0.1"))
        self._webui_token = str(cfg.get("webui_token", ""))
        self._webui_server: object | None = None

    # ════════════════════════════════════════════════════════════════
    # Lifecycle
    # ════════════════════════════════════════════════════════════════

    async def initialize(self):
        await self._disable_builtin_memory()

        # ── Memory subsystem ────────────────────────────────────────────
        ensure_directory_structure()
        try:
            llm_client = self.ctx.get_default_llm_client()
        except Exception:
            llm_client = None
        try:
            fast_llm_client = self.ctx.get_default_fast_llm_client()
        except Exception:
            fast_llm_client = llm_client

        index = MemoryIndex(db_path=self._memory_index_db_path)
        tree_store = TomlTreeStore(index=index)
        profile_store = EntityProfileStore()
        extractor = MemoryExtractor(tree_store, llm_client)
        if fast_llm_client is not None:
            extractor.set_fast_llm_client(fast_llm_client)
        decay_engine = MemoryDecayEngine(tree_store)
        # Apply config-driven thresholds.
        decay_engine.GC_IMPORTANCE_THRESHOLD = self._gc_importance_threshold
        decay_engine.GC_UNACCESSED_DAYS = self._gc_unaccessed_days
        decay_engine.DECAY_INTERVAL_DAYS = self._decay_interval_days

        self.memory_manager = MemoryManager(
            index=index,
            tree_store=tree_store,
            profile_store=profile_store,
            extractor=extractor,
            decay_engine=decay_engine,
            llm_client=llm_client,
            fast_llm_client=fast_llm_client,
        )
        try:
            await self.memory_manager.async_init()
        except Exception as e:
            logger.warning(f"Memory index rebuild on startup failed: {e}")

        # One-shot migration from legacy kiraos.db, if applicable.
        try:
            stats = await legacy_migrate.migrate_legacy_db_if_needed(
                self.memory_manager, self._legacy_db_path
            )
            if stats.get("status") == "migrated":
                logger.info(
                    f"Legacy memory migration done: "
                    f"profiles={stats['profiles']}, events={stats['events']}, "
                    f"skipped={stats['skipped']}"
                )
        except Exception as e:
            logger.warning(f"Legacy migration encountered an error: {e}")

        # Wire tool helpers to our manager.
        memory_tools.set_memory_manager(self.memory_manager)
        memory_tools.set_fast_llm_client(fast_llm_client)

        logger.info("KiraOS memory subsystem ready")

        # ── Skill Router ────────────────────────────────────────────────
        skills = self.skill_router.discover()
        any_with_resources = False
        for skill in skills:
            if skill.name in self._disabled_skills:
                logger.info(f"Skill '{skill.name}' is disabled, skipping")
                continue
            self._register_skill_tool(skill)
            if skill.has_resources():
                any_with_resources = True

        self._command_map = self.skill_router.get_commands(
            enabled_only=set(self._registered_skill_names)
        )

        if skills:
            active = len(self._registered_skill_names)
            logger.info(
                f"Registered {active}/{len(skills)} skill(s): "
                f"{self._registered_skill_names}"
            )
        else:
            logger.info("No skills found (place skill folders in data/skills/)")

        if any_with_resources:
            self._register_resource_tool()

        # ── WebUI ───────────────────────────────────────────────────────
        if self._webui_port > 0 and self.memory_manager:
            try:
                from .web_server import WebUIServer
                self._webui_server = WebUIServer(
                    memory_manager=self.memory_manager,
                    host=self._webui_host,
                    port=self._webui_port,
                    token=self._webui_token,
                )
                await self._webui_server.start()
            except Exception as e:
                logger.warning(f"WebUI startup failed: {e}")
                self._webui_server = None

        logger.info("KiraOS plugin initialized")

    async def _disable_builtin_memory(self):
        """Auto-disable the builtin Simple Memory plugin to prevent conflicts."""
        try:
            mgr = self.ctx.plugin_mgr
            if mgr is None:
                return
            if mgr.is_plugin_enabled(self.BUILTIN_MEMORY_PLUGIN_ID):
                await mgr.set_plugin_enabled(self.BUILTIN_MEMORY_PLUGIN_ID, False)
                logger.warning(
                    "检测到内置记忆插件(Simple Memory)已启用，已自动禁用以避免冲突。"
                )
        except Exception as e:
            logger.warning(f"检查内置记忆插件状态时出错: {e}")

    async def terminate(self):
        # Drain hippocampus tasks before tearing down the DB.
        if self._hippocampus_tasks:
            pending = list(self._hippocampus_tasks)
            try:
                await asyncio.wait_for(
                    asyncio.gather(*pending, return_exceptions=True),
                    timeout=5.0,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    f"[hippocampus] {len(pending)} task(s) still pending at "
                    "shutdown; cancelling"
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
                    pass
            self._hippocampus_tasks.clear()

        if self._webui_server:
            try:
                await self._webui_server.stop()
            except Exception as e:
                logger.warning(f"WebUI shutdown error: {e}")
            self._webui_server = None

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

        memory_tools.set_memory_manager(None)
        memory_tools.set_fast_llm_client(None)

        if self.memory_manager:
            self.memory_manager.close()
        self.memory_manager = None

        logger.info("KiraOS plugin terminated")

    # ════════════════════════════════════════════════════════════════
    # Skill Router (unchanged from prior revision)
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

    def _execute_skill(
        self, skill: SkillInfo, event: KiraMessageBatchEvent, **kwargs
    ) -> str:
        logger.info(f"Loading skill '{skill.name}' instruction (args: {kwargs})")

        instruction = self.skill_router.build_instruction_prompt(skill, kwargs)
        if not instruction:
            return f"Error: skill '{skill.name}' has empty instruction"

        parts = [f"<skill name=\"{skill.name}\">", instruction]

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

        # Inject a small memory context block when available — keeps skills
        # personalised without bloating the prompt.
        if self.memory_manager:
            adapter, sender_id, _ = _primary_speaker(event)
            if sender_id:
                entity_id = f"{adapter}:{sender_id}"
                try:
                    profile_text = asyncio.get_event_loop().run_until_complete(
                        self.memory_manager.get_profile_prompt(entity_id)
                    ) if False else None  # only called inside async — see fallback
                except Exception:
                    profile_text = None
                # Profile injection here would require an async path; skills
                # are sync, so we skip it to keep the contract unchanged. The
                # main inject_context hook already covers the LLM-facing
                # context.

        parts.append("</skill>")
        parts.append("请严格按照上述技能指令执行，直接输出执行结果。")

        return "\n".join(parts)

    def _register_resource_tool(self):
        async def _read_resource(
            event: KiraMessageBatchEvent, *_,
            skill_name: str = "", path: str = "", **__,
        ) -> str:
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
                "参数: skill_name=技能名, path=相对技能根目录的路径。"
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
        logger.info("Registered read_skill_resource tool")

    async def _reload_skills(self):
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
        self._command_map = self.skill_router.get_commands(
            enabled_only=set(self._registered_skill_names)
        )

        if any_with_resources and not self._resource_tool_registered:
            self._register_resource_tool()
        elif not any_with_resources and self._resource_tool_registered:
            try:
                self.ctx.llm_api.unregister_tool("read_skill_resource")
            except Exception as e:
                logger.warning(f"Failed to unregister read_skill_resource: {e}")
            self._resource_tool_registered = False

        logger.info(f"Reloaded skills: {len(self._registered_skill_names)} active")

    @on.im_message(priority=Priority.HIGH)
    async def handle_slash_command(self, event: KiraMessageEvent):
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
    # Memory tools
    # ════════════════════════════════════════════════════════════════

    @register_tool(
        name="memory_add",
        description=(
            "向长期记忆系统添加一条记忆。系统会自动通过 SHA-256 + FTS5 + LLM 三级去重。"
            "用户每次发言后如果提到关于自己的事实(身份/地点/职业/关系/偏好/经历/事件)就调用本工具。"
            "无明确目标用户时省略 entity_id，系统会自动从对话上下文识别。"
        ),
        params={
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "要记录的记忆文本，写成完整陈述句，用具体昵称作主语",
                },
                "entity_id": {
                    "type": "string",
                    "description": "目标用户：支持昵称/曾用名/QQ号/adapter:id 格式，可省略",
                },
                "entity_type": {
                    "type": "string",
                    "enum": ["user", "group", "channel"],
                    "description": "实体类型，默认 user",
                },
                "importance": {
                    "type": "number",
                    "description": "重要性 1-10，默认 5",
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "标签列表（可选）",
                },
                "memory_type": {
                    "type": "string",
                    "enum": ["fact", "reflection"],
                    "description": "fact=单条事实, reflection=高层洞察（默认 fact）",
                },
            },
            "required": ["text"],
        },
    )
    async def memory_add(self, event: KiraMessageBatchEvent, **kwargs) -> str:
        return await memory_tools.memory_add(event, **kwargs)

    @register_tool(
        name="memory_update",
        description=(
            "更新已有记忆。需要 memory_id（可从 memory_search 结果中获取）。"
            "通常仅在用户明确说'我之前说的XX其实是YY'时调用。"
        ),
        params={
            "type": "object",
            "properties": {
                "memory_id": {"type": "string", "description": "记忆ID"},
                "text": {"type": "string", "description": "更新后的文本"},
                "entity_id": {"type": "string", "description": "目标用户（可省略）"},
                "entity_type": {
                    "type": "string",
                    "enum": ["user", "group", "channel"],
                },
                "folder": {
                    "type": "string",
                    "description": "所在目录: facts 或 reflections",
                },
                "importance": {
                    "type": "number",
                    "description": "新的重要性评分（可选）",
                },
            },
            "required": ["memory_id", "text"],
        },
    )
    async def memory_update(self, event: KiraMessageBatchEvent, **kwargs) -> str:
        return await memory_tools.memory_update_tool(event, **kwargs)

    @register_tool(
        name="memory_remove",
        description=(
            "归档（软删除）一条记忆。被归档的记忆会移到 archive/ 目录，不再参与检索。"
            "仅当用户明确说'忘掉我XX的事情'时使用。"
        ),
        params={
            "type": "object",
            "properties": {
                "memory_id": {"type": "string", "description": "记忆ID"},
                "entity_id": {"type": "string", "description": "目标用户（可省略）"},
                "entity_type": {
                    "type": "string",
                    "enum": ["user", "group", "channel"],
                },
                "folder": {
                    "type": "string",
                    "description": "所在目录: facts 或 reflections",
                },
            },
            "required": ["memory_id"],
        },
    )
    async def memory_remove(self, event: KiraMessageBatchEvent, **kwargs) -> str:
        return await memory_tools.memory_remove(event, **kwargs)

    @register_tool(
        name="memory_search",
        description=(
            "语义搜索长期记忆。省略 entity_id 时系统会自动识别对话涉及的用户。"
            "支持逗号分隔的多用户并行搜索（如 '小明,小红'）。"
            "触发例：用户问'你记得我XX吗'、需要 LLM 主动召回相关历史信息时。"
        ),
        params={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索查询文本"},
                "entity_id": {
                    "type": "string",
                    "description": "目标用户（昵称/QQ号/逗号分隔列表/可省略）",
                },
                "entity_type": {
                    "type": "string",
                    "enum": ["user", "group", "channel"],
                },
                "k": {"type": "number", "description": "返回结果数量，默认 5"},
            },
            "required": ["query"],
        },
    )
    async def memory_search(self, event: KiraMessageBatchEvent, **kwargs) -> str:
        return await memory_tools.memory_search(event, **kwargs)

    @register_tool(
        name="profile_view",
        description=(
            "查看实体（用户/群组）画像。无 entity_id 时返回当前发言者画像。"
            "触发例：用户问'你了解我多少'、需要展示画像时。"
        ),
        params={
            "type": "object",
            "properties": {
                "entity_id": {"type": "string", "description": "目标实体（可省略）"},
                "entity_type": {
                    "type": "string",
                    "enum": ["user", "group", "channel"],
                },
            },
            "required": [],
        },
    )
    async def profile_view(self, event: KiraMessageBatchEvent, **kwargs) -> str:
        return await memory_tools.profile_view(event, **kwargs)

    @register_tool(
        name="profile_update",
        description=(
            "更新实体画像。action 选项: add_trait/remove_trait/add_fact/set_name/set_relationship。"
            "set_relationship 时需提供 target（关系对方的 entity_id）。"
        ),
        params={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "add_trait", "remove_trait", "add_fact",
                        "set_name", "set_relationship",
                    ],
                },
                "value": {"type": "string", "description": "操作值"},
                "entity_id": {"type": "string", "description": "目标实体（可省略）"},
                "entity_type": {
                    "type": "string",
                    "enum": ["user", "group", "channel"],
                },
                "target": {
                    "type": "string",
                    "description": "关系对方 entity_id（仅 set_relationship 需要）",
                },
            },
            "required": ["action", "value"],
        },
    )
    async def profile_update(self, event: KiraMessageBatchEvent, **kwargs) -> str:
        return await memory_tools.profile_update(event, **kwargs)

    # ════════════════════════════════════════════════════════════════
    # LLM Hook — inject memory context + skill manifest
    # ════════════════════════════════════════════════════════════════

    @on.llm_request()
    async def inject_context(
        self, event: KiraMessageBatchEvent, req: LLMRequest, *_
    ):
        """Inject recall + profile + skill list before each LLM call."""
        memory_block = ""
        if self.memory_manager:
            adapter, sender_id, nickname = _primary_speaker(event)
            if sender_id:
                entity_id = f"{adapter}:{sender_id}"

                # Bump interaction counter / sync nickname if it changed.
                try:
                    await self.memory_manager.update_user_interaction(
                        entity_id, platform=adapter, nickname=nickname,
                    )
                except Exception as e:
                    logger.debug(f"Profile interaction update failed: {e}")

                latest_text = self._latest_user_text(event)
                if latest_text:
                    try:
                        memories = await self.memory_manager.recall(
                            query=latest_text,
                            entity_id=entity_id,
                            entity_type="user",
                            k=self._memory_top_k,
                        )
                        if memories:
                            memory_block = self.memory_manager.format_recalled_memories(
                                memories
                            )
                    except Exception as e:
                        logger.warning(f"Recall failed: {e}")

                # Profile prompt as a separate, smaller block.
                try:
                    profile_text = await self.memory_manager.get_profile_prompt(
                        entity_id, "user",
                    )
                except Exception as e:
                    logger.debug(f"Profile prompt failed: {e}")
                    profile_text = ""

                if memory_block or profile_text:
                    full = []
                    if profile_text and profile_text != "暂无画像信息":
                        full.append(f"【{nickname or sender_id} 的画像】\n{profile_text}")
                    if memory_block:
                        full.append(f"【相关长期记忆】\n{memory_block}")
                    memory_block = "\n\n".join(full)
                    if (
                        self._memory_inject_max_chars > 0
                        and len(memory_block) > self._memory_inject_max_chars
                    ):
                        memory_block = memory_block[: self._memory_inject_max_chars] + "…"

        skill_line = ""
        if self._registered_skill_names:
            names = []
            for sn in self._registered_skill_names:
                sk = self.skill_router.get_skill(sn)
                if sk:
                    names.append(sk.name)
            if names:
                skill_line = SKILL_FEW_SHOT_HEADER + ", ".join(names) + "\n"

        injected_memory = False
        for p in req.system_prompt:
            if p.name == "memory" and memory_block:
                p.content += f"\n{memory_block}"
                injected_memory = True
            if p.name == "tools" and skill_line:
                p.content += skill_line

        if not injected_memory and memory_block:
            req.system_prompt.append(
                Prompt(memory_block, name="memory", source="kiraos")
            )

        # Standing hint nudging the LLM to actually call memory_add for any
        # new self-facts the user mentions. (Hippocampus also runs after the
        # turn as a safety net.)
        req.system_prompt.append(Prompt(
            "📝 本轮检查: 用户若提到任何自身事实信息"
            "(姓名/地点/职业/关系/偏好/经历), 主动调用 memory_add 记录。"
            "需要召回历史信息时调用 memory_search；查看画像调用 profile_view。",
            name="memory_hint",
            source="kiraos",
        ))

    @staticmethod
    def _latest_user_text(event: KiraMessageBatchEvent) -> str:
        if not getattr(event, "messages", None):
            return ""
        last_msg = event.messages[-1]
        chain = getattr(getattr(last_msg, "message", None), "chain", None)
        if not chain:
            return ""
        from core.chat.message_elements import Text
        return "".join(elem.text for elem in chain if isinstance(elem, Text)).strip()

    # ════════════════════════════════════════════════════════════════
    # Hippocampus — passive scan of every turn for missed facts
    # ════════════════════════════════════════════════════════════════

    @on.step_result()
    async def schedule_hippocampus(
        self, event: KiraMessageBatchEvent, step_result: KiraStepResult
    ):
        """Schedule a background hippocampus pass once per batch.

        Fire-and-forget so we never stall the agent loop; in-flight tasks
        are tracked so terminate() can drain them.
        """
        if not self._hippocampus_enabled or not self.memory_manager:
            return

        eid = id(event)
        if eid in self._processed_event_ids:
            return

        user_text = self._latest_user_text(event)
        if self._hippocampus_skip_keywords and user_text:
            for kw in self._hippocampus_skip_keywords:
                if kw and kw in user_text:
                    logger.info(f"[hippocampus] skip keyword '{kw}' hit, skipped")
                    self._processed_event_ids.add(eid)
                    return
        if not user_text or len(user_text.strip()) < 2:
            self._processed_event_ids.add(eid)
            return

        if len(self._hippocampus_tasks) >= self._hippocampus_max_inflight:
            logger.info(
                f"[hippocampus] inflight cap reached "
                f"({len(self._hippocampus_tasks)}/{self._hippocampus_max_inflight}), "
                "dropping this turn"
            )
            self._processed_event_ids.add(eid)
            return

        self._processed_event_ids.add(eid)
        if len(self._processed_event_ids) > self._max_processed_dedup_size:
            self._processed_event_ids = set(
                list(self._processed_event_ids)[self._max_processed_dedup_size // 2:]
            )

        adapter, sender_id, _ = _primary_speaker(event)
        if not sender_id:
            return
        entity_id = f"{adapter}:{sender_id}"

        assistant_reply = (step_result.raw_output or "").strip()
        conv_text = _conversation_text_from_event(event, assistant_reply)
        if not conv_text:
            return

        task = asyncio.create_task(
            self._run_hippocampus(entity_id, conv_text)
        )
        self._hippocampus_tasks.add(task)
        task.add_done_callback(self._hippocampus_tasks.discard)

    async def _run_hippocampus(self, entity_id: str, conversation_text: str):
        """Bounded background hippocampus pass."""
        if self._hippocampus_semaphore is None:
            self._hippocampus_semaphore = asyncio.Semaphore(
                self._hippocampus_max_inflight
            )
        try:
            async with self._hippocampus_semaphore:
                if not self.memory_manager:
                    return
                try:
                    await asyncio.wait_for(
                        self.memory_manager.process_turn(
                            conversation_text,
                            entity_id=entity_id,
                            entity_type="user",
                        ),
                        timeout=30.0,
                    )
                except asyncio.TimeoutError:
                    logger.warning(
                        f"[hippocampus] process_turn timed out for {entity_id}"
                    )
        except Exception as e:
            logger.exception(f"[hippocampus] error for {entity_id}: {e}")
