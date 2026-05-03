"""
KiraOS Plugin — Combines two OS-level capabilities:

  1. **User Memory (SQLite)**: Per-user profile & event persistence with
     category-aware injection. Tools: memory_update, memory_query, memory_clear.
     Hook: auto-inject filtered memory context into the system prompt.

  2. **Skill Router (Progressive Disclosure)**: scans `data/skills/` for skill
     folders, each in either:
        - SKILL.md (YAML frontmatter + body, Claude-Skills-style), or
        - manifest.json + instruction.md (legacy two-file format).
     When the LLM triggers a skill, the body is loaded and returned as the
     tool result so the main LLM follows the instructions in the SAME tool-loop
     turn — zero extra LLM API calls. A skill folder may also bundle resource
     files under `references/`, `resources/`, `scripts/`, or `data/`; the LLM
     pulls these on demand via the `read_skill_resource` tool, giving a third
     tier of progressive disclosure for long/complex skills.
"""

import asyncio
import hashlib
import json
import re
from typing import Optional

from core.plugin import BasePlugin, logger, on, Priority, register_tool
from core.provider import LLMRequest
from core.prompt_manager import Prompt
from core.chat.message_utils import KiraMessageBatchEvent, KiraMessageEvent, KiraStepResult
from core.utils.path_utils import get_data_path

from .db import UserMemoryDB, _parse_ttl, VALID_CATEGORIES, CATEGORY_PRIORITY, _mask_id
from .skill_router import SkillRouter, SkillInfo

# ────────────────────────────────────────────────────────────────────
#  Auditor (Active Recall) — prompt template
# ────────────────────────────────────────────────────────────────────

AUDITOR_SYSTEM_PROMPT = """\
你是一个用户记忆提取助手。从【最新用户消息】中提取所有应当长期记忆的事实信息。

【硬性规则】
1. 只提取关于"用户自己"的事实信息（身份/地点/职业/关系/偏好/经历）
2. 跳过：纯客套话、玩笑、抱怨/venting、否定句、用户明确说"别记"/"忘了它"
3. 跳过：当前已知信息中已有相同 key 且 value 一致的条目（避免重复写）
4. 输出**严格的 JSON 数组**，无任何额外文本、不要 markdown 代码块包裹
5. 没有可记的就输出 `[]`

【输出格式】
[
  {"key": "<short_chinese_key>", "value": "<concise_value>",
   "category": "basic|preference|social|other",
   "confidence": 0.0-1.0}
]

【字段含义】
- category: basic(基本身份,如昵称/城市/职业)、preference(喜好)、social(关系)、other(其他)
- confidence: 0.8+ = 用户明确陈述; 0.5-0.7 = 推断或语境暗示; 不要低于 0.5

只输出 JSON，禁止任何解释、寒暄、markdown。"""


def _strip_json_fence(text: str) -> str:
    """Strip a leading ``` or ```json fence and trailing ``` if present."""
    if not text:
        return ""
    s = text.strip()
    # Remove leading fence
    m = re.match(r"^```(?:json)?\s*\n?", s, flags=re.IGNORECASE)
    if m:
        s = s[m.end():]
    # Remove trailing fence
    if s.endswith("```"):
        s = s[: -3].rstrip()
    return s

# ════════════════════════════════════════════════════════════════════
#  Prompt Fragments
# ════════════════════════════════════════════════════════════════════

SKILL_FEW_SHOT_HEADER = "技能工具（调用后按返回的指令执行）: "


# ════════════════════════════════════════════════════════════════════
#  Plugin Class
# ════════════════════════════════════════════════════════════════════

class UserMemoryPlugin(BasePlugin):
    """KiraOS Plugin — Memory + Skill Router."""

    def __init__(self, ctx, cfg: dict):
        super().__init__(ctx, cfg)

        # ── Memory config ───────────────────────────────────────────
        db_dir = get_data_path() / "memory"
        self.db_path = str(db_dir / "kiraos.db")
        self.db: UserMemoryDB | None = None
        self.max_events = int(cfg.get("max_events_per_user", 10))
        self.max_profiles = int(cfg.get("max_profiles_per_user", 50))
        self.max_event_keep = int(cfg.get("max_event_keep", 100))
        self.max_context_chars = int(cfg.get("max_context_chars", 500))

        # Categories to push into the system prompt every turn. The remainder
        # are kept queryable through memory_query to save tokens.
        raw_inject = cfg.get("inject_categories", ["basic"])
        if isinstance(raw_inject, str):
            raw_inject = [c.strip() for c in raw_inject.split(",") if c.strip()]
        if not isinstance(raw_inject, list):
            raw_inject = ["basic"]
        # Sentinel "*" or "all" → inject every category (legacy behaviour)
        if any(c in ("*", "all") for c in raw_inject):
            self.inject_categories: Optional[list] = None
        else:
            self.inject_categories = [c for c in raw_inject if c in VALID_CATEGORIES] or ["basic"]

        # ── Skill Router config ─────────────────────────────────────
        skills_dir = cfg.get("skills_dir", "") or str(get_data_path() / "skills")
        self.skill_router = SkillRouter(skills_dir)
        self._registered_skill_names: list[str] = []
        self._disabled_skills: set[str] = set(cfg.get("disabled_skills", []))
        self._command_map: dict[str, SkillInfo] = {}
        self._enable_slash_commands: bool = bool(cfg.get("enable_slash_commands", False))
        self._resource_tool_registered: bool = False

        # ── WebUI config ────────────────────────────────────────────
        self._webui_port = int(cfg.get("webui_port", 0))
        self._webui_host = str(cfg.get("webui_host", "127.0.0.1"))
        self._webui_token = str(cfg.get("webui_token", ""))
        self._webui_server: object | None = None

        # ── Auditor (active recall) config ──────────────────────────
        # When enabled, an extra fast-LLM pass runs after each turn to extract
        # facts the main LLM may have missed. Sees the latest user message,
        # the assistant's just-now reply, and the user's existing profile.
        self._auditor_enabled: bool = bool(cfg.get("memory_auditor_enabled", False))
        self._auditor_model_uuid: str = str(cfg.get("memory_auditor_model_uuid", "") or "")
        raw_skip = cfg.get("memory_auditor_skip_keywords")
        if raw_skip is None:
            raw_skip = ["别记", "忘了它", "随便说说", "随便聊", "开玩笑", "假设说", "假如", "假设"]
        if isinstance(raw_skip, str):
            raw_skip = [s.strip() for s in raw_skip.split(",") if s.strip()]
        self._auditor_skip_keywords: list[str] = list(raw_skip) if isinstance(raw_skip, list) else []
        # Per-event dedup: each batch event is audited at most once even if
        # @on.step_result fires multiple times for it (multi-step tool loops).
        self._audited_event_ids: set[int] = set()
        # Bounded so a long-running session doesn't grow this set without limit;
        # purely defensive — id() values are reused after GC anyway.
        self._max_audit_dedup_size = 10_000
        # Strong references to in-flight auditor tasks. asyncio.create_task
        # only keeps weak references in the event loop, so a task with no
        # external reference can be garbage-collected mid-execution and
        # silently disappear. We park each task here and remove it when done.
        self._auditor_tasks: set[asyncio.Task] = set()
        # Cap concurrent in-flight auditor calls so a traffic spike can't
        # pile up dozens of pending fast-LLM requests, slow down shutdown,
        # or trigger provider rate limits. Above this threshold new audits
        # are dropped (best-effort behaviour — auditor is already an opt-in
        # bonus layer, missing one turn is fine).
        self._auditor_max_inflight: int = max(1, int(cfg.get("memory_auditor_max_inflight", 4)))
        # Belt-and-suspenders: a Semaphore around the actual LLM call so any
        # future caller of _run_auditor() (not just schedule_audit) is also
        # bounded.
        self._auditor_semaphore: Optional[asyncio.Semaphore] = None
        # Confidence ceiling for auditor writes — keeps the main LLM's
        # high-confidence judgements authoritative when both sides write
        # the same key. M6 conflict-detection enforces this naturally:
        # a 0.9 (main LLM) value won't be overwritten by 0.6 (auditor).
        self._auditor_confidence_cap: float = 0.7

    # ════════════════════════════════════════════════════════════════
    #  Lifecycle
    # ════════════════════════════════════════════════════════════════

    async def initialize(self):
        await self._disable_builtin_memory()

        self.db = UserMemoryDB(self.db_path)
        logger.info("User memory database ready")

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

        # Only advertise the resource-tier tool when at least one registered
        # skill ships bundled resources — otherwise it'd be dead weight.
        if any_with_resources:
            self._register_resource_tool()

        # ── Start Memory WebUI ──────────────────────────────────────
        if self._webui_port > 0 and self.db:
            from .web_server import WebUIServer
            self._webui_server = WebUIServer(
                db=self.db,
                host=self._webui_host,
                port=self._webui_port,
                token=self._webui_token,
                max_event_keep=self.max_event_keep,
            )
            await self._webui_server.start()

        logger.info("KiraOS plugin initialized (memory + skill router)")

    BUILTIN_MEMORY_PLUGIN_ID = "kira_plugin_simple_memory"

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
            await self._webui_server.stop()
            self._webui_server = None

        # Drain any in-flight auditor tasks so we don't try to write to a
        # closed DB after this point. 5s ceiling — auditor itself has 15s
        # but during shutdown we'd rather lose late writes than block exit.
        if self._auditor_tasks:
            pending = list(self._auditor_tasks)
            try:
                await asyncio.wait_for(
                    asyncio.gather(*pending, return_exceptions=True),
                    timeout=5.0,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    f"[auditor] {len(pending)} background task(s) still pending at shutdown; cancelling"
                )
                for t in pending:
                    if not t.done():
                        t.cancel()
                # ``Task.cancel()`` only *requests* cancellation — the task may
                # still be running its except/finally blocks (and could touch
                # the DB) until it actually yields. Wait for them to exit
                # cleanly before we proceed to ``self.db.close()``, otherwise
                # an in-flight auditor write can race with database teardown.
                # ``return_exceptions=True`` so a CancelledError doesn't fail
                # the gather; another short timeout caps the worst case.
                try:
                    await asyncio.wait_for(
                        asyncio.gather(*pending, return_exceptions=True),
                        timeout=2.0,
                    )
                except asyncio.TimeoutError:
                    logger.warning(
                        f"[auditor] {sum(1 for t in pending if not t.done())} "
                        "task(s) did not exit after cancel(); leaking — DB close may race"
                    )
            self._auditor_tasks.clear()

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

        if self.db:
            self.db.close()
        self.db = None
        logger.info("KiraOS plugin terminated")

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
        """Load instruction (with substitution + exclude guard) and return as tool_result.

        Mirrors Claude's Skill pattern: the skill body is injected just-in-time
        as the tool's return value, so the main LLM "learns" the skill on the
        fly without a separate API call.
        """
        logger.info(f"Loading skill '{skill.name}' instruction (args: {kwargs})")

        instruction = self.skill_router.build_instruction_prompt(skill, kwargs)
        if not instruction:
            return f"Error: skill '{skill.name}' has empty instruction"

        parts = []
        parts.append(f"<skill name=\"{skill.name}\">")
        parts.append(instruction)

        # If the skill bundles resource files, point the LLM at them.
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

        # Optionally include user memory for context-aware skill execution
        user_id = self._get_primary_user_id(event)
        if self.db and user_id != "unknown":
            mem_ctx = self.db.build_user_context(
                user_id,
                max_events=3,
                max_chars=self.max_context_chars,
                inject_categories=self.inject_categories,
                hint_other_categories=False,
            )
            if mem_ctx:
                parts.append(f"\n<context>\n{mem_ctx}\n</context>")

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

        # Sync resource tool registration with whether any skill needs it
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
    #  Memory — helpers
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
    def _coerce_bool(raw) -> Optional[bool]:
        """Strict-ish boolean coercion for LLM-supplied values.

        Returns the parsed bool, or ``None`` for anything we refuse to guess at
        (so callers can surface the rejection back to the LLM instead of
        silently picking ``True``).

        Accepts: ``True``/``False`` (Python bool), strings ``"true"``/``"false"``
        / ``"1"``/``"0"`` / ``"yes"``/``"no"`` (case-insensitive), and ``None``
        as ``False``. Anything else (numbers other than 0/1, weird strings,
        objects) yields ``None``.
        """
        if raw is None:
            return False
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, (int, float)) and raw in (0, 1):
            return bool(raw)
        if isinstance(raw, str):
            s = raw.strip().lower()
            if s in ("true", "1", "yes"):
                return True
            if s in ("false", "0", "no", ""):
                return False
        return None

    # ════════════════════════════════════════════════════════════════
    #  Memory — Tools
    # ════════════════════════════════════════════════════════════════

    @register_tool(
        name="memory_update",
        description=(
            "用户每次发言后必检：若提到任何关于'用户自己'的事实信息，立即调用本工具记录。"
            "常见触发词（看到任一就记，宁记错不漏过）："
            " 身份: 我叫/我是/我姓/年龄/性别/生日/星座; "
            " 地点: 在/住在/来自/老家/工作地; "
            " 职业: 工作/学校/专业/职位/行业; "
            " 关系: 男友/女友/老公/老婆/家人/宠物; "
            " 偏好: 喜欢/讨厌/爱吃/不吃/口味/兴趣; "
            " 经历: 刚才/今天/昨天/最近/上周做了 X。"
            "判断标准: 信息'明天再聊还希望你记得'就记。"
            "确定信息用 confidence=0.8+, 不确定用 0.3-0.5; 低置信度记错没关系，后续高置信度会覆盖。"
            "**只有完全无事实信息的纯客套('你好'/'哈哈'/'好的'/'谢谢') 才跳过。**"
            "示例: 用户说'我叫小明,在北京,今天跑完半马' → "
            "[{op:'set',key:'昵称',value:'小明',category:'basic',confidence:0.9},"
            "{op:'set',key:'城市',value:'北京',category:'basic',confidence:0.9},"
            "{op:'event',value:'完成半马',tag:'milestone'}]。"
            "字段说明: "
            " category: basic(基本信息)/preference(偏好)/social(社交)/other; "
            " confidence: 0-1, set 时必填(不允许默认); "
            " tag(event 用): milestone(里程碑)/daily(日常)/mood(情绪) 等任意短词; "
            " ttl: 临时信息可设过期时间如'30d','7d','12h'; "
            " force: 仅覆盖更高置信度的现有值时设 true; "
            " user_id: 群聊场景下指定目标发言者(必须是当前对话中的人)。"
            "同 batch 内同 (op,key) 只保留最后一个。"
        ),
        params={
            "type": "object",
            "properties": {
                "operations": {
                    "type": "array",
                    "description": "操作列表",
                    "items": {
                        "type": "object",
                        "properties": {
                            "op": {
                                "type": "string",
                                "enum": ["set", "event", "del"],
                                "description": "set=设置画像, event=记录事件, del=删除画像"
                            },
                            "key": {"type": "string", "description": "画像键名(set/del时必填)"},
                            "value": {"type": "string", "description": "画像值(set时)或事件描述(event时)"},
                            "category": {
                                "type": "string",
                                "enum": ["basic", "preference", "social", "other"],
                                "description": "画像分类(set时可选, 默认basic)"
                            },
                            "confidence": {
                                "type": "number",
                                "description": "置信度0-1(set时必填; 不传将被拒绝)"
                            },
                            "tag": {
                                "type": "string",
                                "description": "事件标签(event时可选, 如milestone/daily/mood)"
                            },
                            "ttl": {
                                "type": "string",
                                "description": "过期时间(set时可选), 如'30d','7d','12h'"
                            },
                            "force": {
                                "type": "boolean",
                                "description": "强制覆盖更高置信度的现值(set时可选)"
                            },
                            "user_id": {
                                "type": "string",
                                "description": "目标用户ID(群聊场景可选; 缺省=最后发言者; 必须是当前对话中的某个发言者)"
                            }
                        },
                        "required": ["op"]
                    }
                }
            },
            "required": ["operations"]
        }
    )
    async def memory_update(self, event: KiraMessageBatchEvent, operations: list) -> str:
        if not self.db:
            return "Error: memory database not initialized"

        primary_uid = self._get_primary_user_id(event)
        if primary_uid == "unknown":
            return "Error: cannot determine user_id"
        # Whitelist of senders: per-op user_id is only honoured if it appears here.
        sender_set = set(self._extract_user_ids(event))
        if primary_uid != "unknown":
            sender_set.add(primary_uid)

        # ── M1: dedupe within batch by (op, key, target_uid). Last wins.
        # event ops are NOT deduped (multiple distinct events are legitimate).
        seen: dict = {}
        deduped: list = []
        dropped = 0
        for raw in operations:
            if not isinstance(raw, dict):
                deduped.append(raw)  # let the per-item validator complain
                continue
            op = raw.get("op", "")
            key = raw.get("key", "")
            # Only fall back to primary when ``user_id`` is missing/null —
            # ``or primary_uid`` would silently substitute on falsy values
            # (``""``, ``0``, ``False``) that the LLM might emit, hiding bad
            # input from the per-item validator below.
            raw_target = raw.get("user_id")
            target = primary_uid if raw_target is None else raw_target
            if op in ("set", "del") and key:
                dedup_key = (op, key, target)
                if dedup_key in seen:
                    deduped[seen[dedup_key]] = raw  # later op wins
                    dropped += 1
                    continue
                seen[dedup_key] = len(deduped)
            deduped.append(raw)

        results = []
        touched_users: set = set()  # for the post-summary
        for item in deduped:
            if not isinstance(item, dict):
                results.append("skip: invalid operation (not an object)")
                continue
            op = item.get("op", "")
            key = item.get("key", "")
            value = item.get("value", "")

            # ── M5: per-op user_id with sender whitelist
            # Only None/missing falls back; falsy values (""/0/False) flow
            # through to the validator below so bad LLM input is surfaced
            # instead of silently routed to the primary speaker.
            raw_target = item.get("user_id")
            target_uid = primary_uid if raw_target is None else raw_target
            if not isinstance(target_uid, str) or not target_uid:
                results.append("skip: invalid target user_id")
                continue
            if target_uid not in sender_set:
                results.append(
                    f"skip: user_id '{target_uid}' is not a current speaker "
                    f"(allowed: {sorted(sender_set)})"
                )
                continue

            if op == "set":
                if not key or not value:
                    results.append("skip: set requires key+value")
                    continue
                category = item.get("category", "basic")
                if not isinstance(category, str) or category not in VALID_CATEGORIES:
                    category = "basic"

                # ── M3: confidence is required on set. We accept any number in
                # [0, 1]; absence (or non-numeric) is rejected so the LLM has to
                # think about how sure it is rather than defaulting to 0.5.
                raw_conf = item.get("confidence")
                if raw_conf is None:
                    results.append(f"skip: set {key} requires explicit 'confidence' (0-1)")
                    continue
                try:
                    confidence = float(raw_conf)
                except (TypeError, ValueError):
                    results.append(f"skip: set {key} confidence must be numeric, got {raw_conf!r}")
                    continue

                ttl = item.get("ttl")
                parsed_ttl = _parse_ttl(ttl) if ttl else None
                # ``bool()`` on a non-empty string is True, so ``bool("false")``
                # would silently bypass M6's conflict protection. Accept only
                # real booleans, or strings that are explicitly truthy/falsy.
                force = self._coerce_bool(item.get("force", False))
                if force is None:
                    results.append(
                        f"skip: set {key} 'force' must be boolean (got {item.get('force')!r})"
                    )
                    continue

                status, info = self.db.upsert_with_limit(
                    target_uid, key, value,
                    max_profiles=self.max_profiles,
                    confidence=confidence,
                    category=category,
                    expires_at=parsed_ttl,
                    force=force,
                )
                touched_users.add(target_uid)

                target_tag = "" if target_uid == primary_uid else f" @{target_uid}"
                if status == "limit_exceeded":
                    results.append(
                        f"skip: profile limit ({self.max_profiles}) for {target_uid}"
                    )
                elif status == "conflict":
                    results.append(
                        f"conflict {key}{target_tag}: {info['hint']}"
                    )
                elif status == "truncated":
                    ttl_note = f" (expires: {ttl})" if parsed_ttl else ""
                    results.append(
                        f"set {key}={value[:60]}…{target_tag} [{category}]{ttl_note} "
                        f"(value truncated from {info['truncated_from']} chars)"
                    )
                else:
                    ttl_note = f" (expires: {ttl})" if parsed_ttl else ""
                    forced = " [forced]" if force else ""
                    results.append(f"{status} {key}={value}{target_tag} [{category}]{ttl_note}{forced}")

            elif op == "event":
                if not value:
                    results.append("skip: event requires value")
                    continue
                tag = item.get("tag")
                self.db.save_event(target_uid, value, tag=tag)
                self.db.cleanup_old_events(target_uid, keep=self.max_event_keep)
                touched_users.add(target_uid)
                target_tag = "" if target_uid == primary_uid else f" @{target_uid}"
                tag_str = f" #{tag}" if tag else ""
                results.append(f"event{tag_str}{target_tag}: {value}")

            elif op == "del":
                if not key:
                    results.append("skip: del requires key")
                    continue
                removed = self.db.remove_profile(target_uid, key)
                touched_users.add(target_uid)
                target_tag = "" if target_uid == primary_uid else f" @{target_uid}"
                results.append(f"del {key}{target_tag}: {'ok' if removed else 'not found'}")

            else:
                results.append(f"skip: unknown op '{op}'")

        # Post-summary: show current profile of the primary user so the LLM
        # can self-check for contradictions next turn.
        summary_suffix = ""
        if self.db and primary_uid != "unknown":
            profiles = self.db.get_profiles(primary_uid)
            if profiles:
                top = profiles[:5]
                kvs = ", ".join(f"{k}={v}" for k, v, *_ in top)
                more = f" (+{len(profiles) - 5})" if len(profiles) > 5 else ""
                summary_suffix = f"\n当前画像({primary_uid}): {kvs}{more}"

        dedup_note = f" [{dropped} 重复操作已合并]" if dropped else ""
        logger.info(
            f"memory_update for {_mask_id(primary_uid)}: {len(operations)} ops "
            f"({dropped} deduped) → {len(results)} results"
        )
        return (
            f"已完成 {len(results)} 项记忆操作{dedup_note}: "
            + "; ".join(results)
            + summary_suffix
        )

    @register_tool(
        name="memory_query",
        description=(
            "查询用户记忆。无参数时返回全部画像和近期事件；"
            "传 category 可只查指定分类（basic/preference/social/other），"
            "适合在 system prompt 里只注入了 basic 类，需要其他类信息时主动调用。"
            "群聊场景可传 user_id 指定查询哪个发言者(必须是当前对话中的用户)。"
            "触发例: 用户问 '你记得我什么'、'你知道我的信息吗'、'我的画像'，"
            "或对话上下文显示需要 preference/social 类信息时。"
        ),
        params={
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "enum": ["basic", "preference", "social", "other"],
                    "description": "可选: 仅查询指定分类的画像（不传则返回全部）"
                },
                "user_id": {
                    "type": "string",
                    "description": "可选: 群聊场景查指定发言者（必须是当前对话中的用户）"
                }
            },
            "required": []
        }
    )
    async def memory_query(self, event: KiraMessageBatchEvent,
                           category: Optional[str] = None,
                           user_id: Optional[str] = None, **_) -> str:
        if not self.db:
            return "Error: memory database not initialized"
        primary_uid = self._get_primary_user_id(event)
        if primary_uid == "unknown" and not user_id:
            return "Error: cannot determine user_id"
        # Only fall back when user_id wasn't explicitly given (None). An LLM
        # call passing ``user_id=""`` should flow into the whitelist check
        # below and get rejected, rather than silently routing to the primary
        # speaker as ``or primary_uid`` would have done.
        target = primary_uid if user_id is None else user_id
        if user_id is not None:
            sender_set = set(self._extract_user_ids(event))
            if primary_uid != "unknown":
                sender_set.add(primary_uid)
            if user_id not in sender_set:
                return (
                    f"Error: user_id '{user_id}' is not a current speaker "
                    f"(allowed: {sorted(sender_set)})"
                )
        if category is not None and category not in VALID_CATEGORIES:
            return f"Error: invalid category '{category}', must be one of {sorted(VALID_CATEGORIES)}"
        return self.db.get_all_profiles_formatted(
            target,
            max_events=self.max_events,
            category=category,
        )

    @register_tool(
        name="consolidate_memory",
        description=(
            "记忆压缩/反思工具：当用户的事件日志累积较多（超过 ~20 条）或用户主动"
            "请求'帮我整理记忆/总结一下我的事件'时调用。返回最近若干条事件，"
            "要求 LLM 在同一轮内识别重复模式并通过 memory_update 将稳定模式提升"
            "为长期 profile 条目。不要在普通对话中频繁调用。"
        ),
        params={
            "type": "object",
            "properties": {
                "n_events": {
                    "type": "integer",
                    "description": "回顾最近多少条事件（默认 30，上限 100）"
                }
            },
            "required": []
        }
    )
    async def consolidate_memory(self, event: KiraMessageBatchEvent,
                                 n_events: int = 30, **_) -> str:
        if not self.db:
            return "Error: memory database not initialized"
        user_id = self._get_primary_user_id(event)
        if user_id == "unknown":
            return "Error: cannot determine user_id"
        try:
            n = int(n_events)
        except (TypeError, ValueError):
            n = 30
        n = max(5, min(100, n))

        events = self.db.get_recent_events(user_id, limit=n)
        if not events:
            return "该用户暂无事件记录，无需整理。"

        # Pull the existing profile too so the consolidation knows what's
        # already captured and won't propose duplicates.
        profiles = self.db.get_profiles(user_id)

        lines = [f"<consolidation user_id=\"{user_id}\">"]
        lines.append(f"\n## 最近 {len(events)} 条事件\n")
        for summary, ts, tag in events:
            tag_s = f" [#{tag}]" if tag else ""
            lines.append(f"- {ts[:10]}{tag_s} {summary}")

        if profiles:
            lines.append("\n## 当前长期画像（避免重复创建）\n")
            for k, v, _, conf, cat, _ in profiles[:30]:
                lines.append(f"- [{cat}] {k} = {v} (conf={conf:.2f})")

        lines.append("""
## 你的任务
1. 在以上事件中识别**至少出现 3 次**的稳定模式（运动习惯、兴趣、作息、关系、技能…）。
2. 跳过已经存在于"当前长期画像"里的内容。
3. 对于每个识别出的稳定模式，调用 `memory_update` 创建对应的 profile：
   - `op: "set"`, `category: "preference"|"social"|"basic"`,
   - `confidence`: 出现 3-4 次用 0.6, 出现 5-9 次用 0.75, ≥10 次用 0.9
   - `value` 用一句话概括该模式
4. 输出一段简短的中文总结说明你提升了哪些条目；如果没有可提升的模式，直接说"没有发现新的稳定模式"。
5. 不要逐条复述事件本身。
</consolidation>""")
        return "\n".join(lines)

    @register_tool(
        name="memory_clear",
        description="清除用户的全部记忆。仅当用户明确要求'忘记我'、'清除我的记忆'、'删除我的所有信息'时调用。",
        params={"type": "object", "properties": {}, "required": []}
    )
    async def memory_clear(self, event: KiraMessageBatchEvent, **_) -> str:
        if not self.db:
            return "Error: memory database not initialized"
        user_id = self._get_primary_user_id(event)
        if user_id == "unknown":
            return "Error: cannot determine user_id"
        profiles_del, events_del = self.db.clear_user_memory(user_id)
        logger.info(f"memory_clear for {_mask_id(user_id)}: {profiles_del} profiles, {events_del} events deleted")
        return f"已清除全部记忆: 删除了 {profiles_del} 条画像和 {events_del} 条事件记录。"

    # ════════════════════════════════════════════════════════════════
    #  LLM Hook — inject memory context + skill manifest descriptions
    # ════════════════════════════════════════════════════════════════

    @on.llm_request()
    async def inject_context(self, event: KiraMessageBatchEvent, req: LLMRequest, *_):
        """Inject filtered per-user memory + skill list before each LLM call.

        Memory: only categories listed in `inject_categories` (default: basic)
        are pushed into the system prompt; remaining categories surface as a
        one-line hint pointing the LLM at `memory_query(category=...)`.
        """
        memory_context = ""
        if self.db:
            user_ids = self._extract_user_ids(event)
            if user_ids:
                memory_blocks = []
                for uid in user_ids:
                    ctx_str = self.db.build_user_context(
                        uid,
                        max_events=self.max_events,
                        max_chars=self.max_context_chars,
                        inject_categories=self.inject_categories,
                        hint_other_categories=True,
                    )
                    if ctx_str:
                        memory_blocks.append(ctx_str)
                if memory_blocks:
                    memory_context = "\n".join(memory_blocks)

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
            if p.name == "memory" and memory_context:
                p.content += f"\n{memory_context}"
                injected_memory = True
            if p.name == "tools" and skill_line:
                p.content += skill_line

        # If no dedicated "memory" section exists, append a fresh one rather
        # than piggy-backing on whatever happens to be the last segment
        # (which is typically "tools" — appending memory there mixes two
        # unrelated kinds of content and confuses the model).
        if not injected_memory and memory_context:
            req.system_prompt.append(
                Prompt(memory_context, name="memory", source="kiraos")
            )

        # ── B: per-turn active-recall hint ─────────────────────────
        # The hint sits next to the memory data so the LLM, while reading
        # what it already knows about the user, also sees a standing
        # instruction to record any *new* facts mentioned this turn.
        # Tool-description rewriting (A) plus this hint reliably nudges
        # otherwise-conservative models into actually calling memory_update.
        req.system_prompt.append(Prompt(
            "📝 本轮检查: 用户若提到任何自身事实信息"
            "(姓名/地点/职业/关系/偏好/经历), 主动调用 memory_update 记录。"
            "宁记错不漏过——低置信度记下来，后续会被高置信度覆盖。",
            name="memory_hint",
            source="kiraos",
        ))

    # ════════════════════════════════════════════════════════════════
    #  Auditor (C) — passive scan of every turn for missed facts
    # ════════════════════════════════════════════════════════════════

    @on.step_result()
    async def schedule_audit(self, event: KiraMessageBatchEvent,
                             step_result: KiraStepResult):
        """Schedule a background auditor pass for this batch.

        Runs at most once per ``KiraMessageBatchEvent`` even if step_result
        fires multiple times (multi-step tool loops). Fully async — does
        not block the agent loop.
        """
        if not self._auditor_enabled or not self.db:
            return
        eid = id(event)
        if eid in self._audited_event_ids:
            return
        # Skip-keyword pre-filter: cheap text match, avoids paying for an LLM
        # call when the user explicitly opted out of recording this turn.
        user_text = self._extract_latest_user_text(event)
        if self._auditor_skip_keywords and user_text:
            for kw in self._auditor_skip_keywords:
                if kw and kw in user_text:
                    logger.info(f"[auditor] skip keyword '{kw}' hit, audit skipped")
                    self._audited_event_ids.add(eid)
                    return
        if not user_text or len(user_text.strip()) < 2:
            # Nothing meaningful to audit
            self._audited_event_ids.add(eid)
            return

        # Inflight cap. Each auditor call can take up to 15s (model timeout),
        # so an unbounded burst would queue tasks faster than they drain,
        # slow down terminate(), and could trip provider rate-limits. Drop
        # excess turns at the door — the auditor is best-effort anyway.
        if len(self._auditor_tasks) >= self._auditor_max_inflight:
            logger.info(
                f"[auditor] inflight cap reached "
                f"({len(self._auditor_tasks)}/{self._auditor_max_inflight}), "
                "dropping this turn"
            )
            self._audited_event_ids.add(eid)
            return

        self._audited_event_ids.add(eid)
        # Defensive cap on the dedup set so a long session can't bloat it.
        if len(self._audited_event_ids) > self._max_audit_dedup_size:
            # Drop an arbitrary half to keep the set bounded.
            # NOTE: Python ``set`` is unordered (only ``dict`` gained
            # insertion-order semantics in 3.7), so the slice below removes
            # a hash-dependent half — *not* the oldest entries. That's fine
            # for our purpose: the dedup is best-effort anyway, since
            # ``id()`` values are recycled after GC. We only need the size
            # to stay within ``_max_audit_dedup_size``.
            self._audited_event_ids = set(
                list(self._audited_event_ids)[self._max_audit_dedup_size // 2:]
            )

        # Snapshot what we need so the background task is independent of
        # event mutation later in the loop.
        user_id = self._get_primary_user_id(event)
        if user_id == "unknown":
            return
        assistant_reply = (step_result.raw_output or "").strip()

        # Fire-and-forget. We don't await so the step_result handler chain
        # doesn't stall the main agent loop. Errors are logged inside.
        # The task ref must be retained or asyncio's weak-ref bookkeeping can
        # GC it mid-flight; we discard it once it finishes.
        task = asyncio.create_task(
            self._run_auditor(user_id=user_id,
                              user_text=user_text,
                              assistant_reply=assistant_reply)
        )
        self._auditor_tasks.add(task)
        task.add_done_callback(self._auditor_tasks.discard)

    @staticmethod
    def _extract_latest_user_text(event: KiraMessageBatchEvent) -> str:
        """Concatenate all Text elements from the latest user message in the batch."""
        if not event.messages:
            return ""
        last_msg = event.messages[-1]
        chain = getattr(getattr(last_msg, "message", None), "chain", None)
        if not chain:
            return ""
        from core.chat.message_elements import Text
        parts = []
        for elem in chain:
            if isinstance(elem, Text):
                parts.append(elem.text or "")
        return "".join(parts).strip()

    def _get_auditor_client(self):
        """Pick the LLM client to drive the auditor.

        Priority:
          1. Explicit ``memory_auditor_model_uuid`` from config
          2. ctx.get_default_fast_llm_client()
          3. ctx.get_default_llm_client() as last resort
        """
        if self._auditor_model_uuid:
            try:
                client = self.ctx.get_llm_client(model_uuid=self._auditor_model_uuid)
                if client is not None:
                    return client
                logger.warning(
                    f"[auditor] configured model '{self._auditor_model_uuid}' "
                    "not available, falling back to fast LLM"
                )
            except Exception as e:
                logger.warning(f"[auditor] failed to load configured model: {e}")
        try:
            return self.ctx.get_default_fast_llm_client()
        except Exception:
            try:
                return self.ctx.get_default_llm_client()
            except Exception:
                return None

    async def _run_auditor(self, *, user_id: str, user_text: str,
                           assistant_reply: str) -> None:
        """Background pass: ask the fast LLM to extract memorable facts and write them.

        Failures are logged and swallowed — never propagate to the main loop.
        """
        try:
            client = self._get_auditor_client()
            if client is None:
                logger.warning("[auditor] no LLM client available, skipping audit")
                return
            if not self.db:
                return

            # Snapshot the user's existing profile so the auditor can dedupe
            existing = self.db.get_profiles(user_id)
            profile_lines = [
                f"- {k} = {v} [{cat}] (conf {conf:.2f})"
                for (k, v, _, conf, cat, _) in existing[:50]
            ]
            profile_block = "\n".join(profile_lines) if profile_lines else "(空)"

            payload = (
                f"【当前已知用户画像】\n{profile_block}\n\n"
                f"【最新用户消息】\n{user_text}\n\n"
                f"【助手刚才的回复（用作语境）】\n"
                f"{assistant_reply[:500] if assistant_reply else '(无)'}\n"
            )

            req = LLMRequest(
                system_prompt=[Prompt(AUDITOR_SYSTEM_PROMPT, name="auditor", source="kiraos")],
                user_prompt=[Prompt(payload, name="auditor_payload", source="kiraos")],
            )
            req.assemble_prompt()

            # Lazily build the semaphore on first use — Semaphore must be
            # created on the running event loop, which __init__ doesn't have
            # access to. ``schedule_audit``'s inflight check is the primary
            # throttle; this is a secondary cap so any future direct caller
            # of ``_run_auditor`` (without the inflight check) still can't
            # exceed the configured concurrency.
            if self._auditor_semaphore is None:
                self._auditor_semaphore = asyncio.Semaphore(self._auditor_max_inflight)

            try:
                async with self._auditor_semaphore:
                    resp = await asyncio.wait_for(client.chat(req), timeout=15.0)
            except asyncio.TimeoutError:
                logger.warning(f"[auditor] LLM call timed out for user {_mask_id(user_id)}")
                return
            text = (resp.text_response or "").strip()
            if not text:
                return

            extracted = self._parse_auditor_output(text)
            if not extracted:
                logger.info(f"[auditor] {_mask_id(user_id)}: no facts extracted")
                return

            # Write to DB. Each entry goes through upsert_with_limit so M6
            # conflict-detection (and the confidence cap) protect existing
            # higher-confidence values written by the main LLM.
            written = 0
            skipped = 0
            for item in extracted:
                key = (item.get("key") or "").strip()
                value = (item.get("value") or "").strip()
                if not key or not value:
                    skipped += 1
                    continue
                category = item.get("category", "other")
                if category not in VALID_CATEGORIES:
                    category = "other"
                try:
                    confidence = float(item.get("confidence", 0.5))
                except (TypeError, ValueError):
                    confidence = 0.5
                # Cap confidence so the auditor never out-confidences the main LLM
                confidence = max(0.0, min(self._auditor_confidence_cap, confidence))
                status, info = self.db.upsert_with_limit(
                    user_id, key, value,
                    max_profiles=self.max_profiles,
                    confidence=confidence,
                    category=category,
                )
                if status in ("set", "updated", "truncated"):
                    written += 1
                else:
                    skipped += 1
                    logger.info(
                        f"[auditor] {_mask_id(user_id)}: {_mask_id(key)} skipped "
                        f"({status}: {info})"
                    )

            logger.info(
                f"[auditor] {_mask_id(user_id)}: extracted {len(extracted)}, "
                f"written {written}, skipped {skipped}"
            )
        except Exception as e:
            # Don't let a buggy auditor take down the main turn. Log and move on.
            logger.exception(f"[auditor] error for {_mask_id(user_id)}: {e}")

    @staticmethod
    def _parse_auditor_output(text: str) -> list[dict]:
        """Best-effort parse of the auditor's JSON output.

        Tolerates code-fence wrapping and extra leading/trailing prose.
        Returns ``[]`` on any error.
        """
        cleaned = _strip_json_fence(text)
        # If the model added prose around the JSON, try to find the first [...] block
        if not cleaned.startswith("["):
            start = cleaned.find("[")
            end = cleaned.rfind("]")
            if start != -1 and end > start:
                cleaned = cleaned[start: end + 1]
        try:
            data = json.loads(cleaned)
        except (json.JSONDecodeError, ValueError) as e:
            # The auditor output contains the user facts the model just
            # extracted (nicknames, addresses, relationships, ...). Logging
            # the raw text — even truncated — would persist that to the log
            # file. Record only length + a content hash so we can still
            # correlate repeated failures across runs without leaking PII.
            digest = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:12]
            logger.warning(
                f"[auditor] failed to parse JSON: {e} "
                f"(text_len={len(text)}, sha256_12={digest})"
            )
            return []
        if not isinstance(data, list):
            logger.warning(f"[auditor] expected JSON array, got {type(data).__name__}")
            return []
        out = []
        for item in data:
            if isinstance(item, dict):
                out.append(item)
        return out
