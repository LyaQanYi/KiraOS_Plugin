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

from .anti_repeat import AntiRepeatCorpus, format_anti_repeat_hint
from .cognition.evidence import EvidenceConfig
from .cognition.facts import fact_hash, importance_from_text, is_dedup_candidate
from .cognition.reconciler import Reconciler
from .db import UserMemoryDB, _parse_ttl, VALID_CATEGORIES, CATEGORY_PRIORITY, _mask_id
from .embeddings import EmbeddingService
from .recall import MemoryRecaller, RecallConfig
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
        db_dir = get_data_path() / "memory"
        self.db_path = str(db_dir / "kiraos.db")
        self.db: UserMemoryDB | None = None
        self._enable_fts5: bool = bool(cfg.get("enable_fts5", True))
        # ── Phase 2: recall pipeline knobs ──────────────────────────
        # All recall features default OFF except FTS5 (zero-dep). The
        # plugin still works when each is disabled — recall.py
        # transparently skips absent stages.
        self._enable_embedding: bool = bool(cfg.get("enable_embedding", False))
        self._enable_llm_rerank: bool = bool(cfg.get("enable_llm_rerank", False))
        self._reranker_model_uuid: str = str(
            cfg.get("memory_reranker_model_uuid", "") or ""
        )
        self._recaller: Optional[MemoryRecaller] = None
        self._embedding_service: Optional[EmbeddingService] = None
        # ── Phase 3a: cognition + evidence ──────────────────────────
        # Fact dedup is on by default — it's purely a write-side
        # optimization (deduplicate restatements of the same event) and
        # shouldn't surprise existing users. Evidence math is harmless
        # until Phase 3b lights up the reconciler that uses it.
        self._enable_fact_dedup: bool = bool(cfg.get("enable_fact_dedup", True))
        self._evidence_config = EvidenceConfig(
            rein_half_life_days=float(
                cfg.get("evidence_half_life_days_rein", 14.0)
            ),
            disp_half_life_days=float(
                cfg.get("evidence_half_life_days_disp", 7.0)
            ),
            promoted_threshold=float(
                cfg.get("evidence_promoted_threshold", 1.0)
            ),
            confirmed_threshold=float(
                cfg.get("evidence_confirmed_threshold", 0.3)
            ),
            archive_threshold=float(
                cfg.get("evidence_archive_threshold", -0.5)
            ),
        )
        # ── Phase 3b: reflection synthesis + auto-promote ───────────
        # Both off by default — synthesis costs one LLM call per
        # eligible audit cycle, and auto-promotion writes to the
        # persona table without a human-in-the-loop. Users opt in
        # via schema.json when they're ready.
        self._enable_reflection_synthesis: bool = bool(
            cfg.get("enable_reflection_synthesis", False)
        )
        self._enable_auto_promotion: bool = bool(
            cfg.get("enable_auto_promotion", False)
        )
        self._reflection_min_facts: int = max(
            2, int(cfg.get("reflection_min_facts", 5))
        )
        self._reflection_promote_age_days: float = float(
            cfg.get("reflection_promote_age_days", 3.0)
        )
        # 30-min tick by default; bounded to [60s, 24h] so a hand-
        # edited config can't accidentally either hammer the DB
        # every second or stall promotion for weeks.
        self._promote_tick_seconds: float = max(
            60.0, min(86400.0, float(
                cfg.get("auto_promote_tick_seconds", 1800.0)
            ))
        )
        self._reconciler: Optional[Reconciler] = None
        self._promote_tick_task: Optional[asyncio.Task] = None
        self._reconciler_tasks: set[asyncio.Task] = set()
        # ── Phase 4: anti-repeat + embedding backfill ───────────────
        # Off by default. When enabled, ``inject_context`` runs the
        # corpus DF check before each LLM call and adds a one-line
        # hint listing over-used tokens.
        self._enable_anti_repeat: bool = bool(cfg.get("enable_anti_repeat", False))
        self._anti_repeat_lookback: int = max(2, int(
            cfg.get("anti_repeat_lookback_turns", 10)
        ))
        self._anti_repeat_df_threshold: int = max(2, int(
            cfg.get("anti_repeat_df_threshold", 5)
        ))
        # Corpus is constructed regardless of the flag so we can be
        # turned on mid-session without losing the warm-up window —
        # ``record`` is cheap. The hint just stays empty until enabled.
        self._anti_repeat = AntiRepeatCorpus(max_size=100)
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

        self.db = UserMemoryDB(self.db_path, enable_fts5=self._enable_fts5)
        logger.info(
            f"User memory database ready (FTS5={'on' if self.db.fts5_enabled else 'off'})"
        )

        # ── Phase 2: assemble the recall pipeline ───────────────────
        # The embedding service is lazy — it doesn't touch any backend
        # until first use, so it's cheap to construct even when disabled.
        # The LLM rerank provider reuses the auditor's client-resolution
        # chain (configured uuid → fast → default) unless a separate
        # ``memory_reranker_model_uuid`` is set, in which case we try
        # that first and fall back to the auditor chain on failure.
        self._embedding_service = EmbeddingService(
            enabled=self._enable_embedding,
            llm_client_provider=self._safe_default_llm_client,
        )
        self._recaller = MemoryRecaller(
            self.db,
            embedding_service=self._embedding_service,
            llm_client_provider=self._get_reranker_client,
            config=RecallConfig(
                enable_embedding=self._enable_embedding,
                enable_llm_rerank=self._enable_llm_rerank,
            ),
        )

        # ── Phase 3b: build the reconciler ──────────────────────────
        # We always construct it (cheap), but only fire its stages
        # when the corresponding feature flag is on. Stage 2 piggy-
        # backs on schedule_audit; Stage 3 runs on its own tick.
        self._reconciler = Reconciler(
            self.db,
            llm_client_provider=self._get_auditor_client,
            evidence_config=self._evidence_config,
            min_facts=self._reflection_min_facts,
            promote_age_days=self._reflection_promote_age_days,
            max_inflight=self._auditor_max_inflight,
            auditor_confidence_cap=self._auditor_confidence_cap,
        )
        if self._enable_auto_promotion:
            # Single long-running coroutine that wakes every
            # ``promote_tick_seconds``. Held in self so terminate()
            # can cancel it on shutdown.
            self._promote_tick_task = asyncio.create_task(
                self._auto_promote_loop()
            )
            logger.info(
                f"Reflection auto-promotion tick started "
                f"(every {int(self._promote_tick_seconds)}s, "
                f"age threshold {self._reflection_promote_age_days}d)"
            )

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

    async def _auto_promote_loop(self):
        """Long-running coroutine: every ``_promote_tick_seconds``,
        sweep pending reflections and auto-promote the eligible ones.

        Exits cleanly on cancel (terminate path). Per-tick errors are
        swallowed — a bad tick must not kill the loop.
        """
        try:
            while True:
                try:
                    await asyncio.sleep(self._promote_tick_seconds)
                except asyncio.CancelledError:
                    return
                if not self.db or self._reconciler is None:
                    return
                try:
                    result = await self._reconciler.promote_pending()
                    if result.promotions or result.denials or result.errors:
                        logger.info(
                            f"[reconciler] promote tick: "
                            f"+{result.promotions} promoted, "
                            f"{result.denials} blocked by disp, "
                            f"{len(result.errors)} errors"
                        )
                except Exception as exc:
                    # Catch-all: never let one bad tick kill the loop.
                    logger.warning(f"[reconciler] promote tick errored: {exc}")
        except asyncio.CancelledError:
            return

    async def _maybe_synthesize_reflections(self, user_id: str) -> None:
        """Stage-2 hook: called after the auditor finishes for a user.
        Cheap when the user hasn't accumulated enough unabsorbed facts;
        does one LLM call when they have. Fire-and-forget — errors
        are logged and swallowed inside the reconciler.
        """
        if not self._enable_reflection_synthesis or self._reconciler is None:
            return
        try:
            result = await self._reconciler.synthesize_if_ready(user_id)
            if result.reflections_persisted:
                logger.info(
                    f"[reconciler] synthesis for {_mask_id(user_id)}: "
                    f"{result.facts_considered} facts → "
                    f"{result.reflections_persisted} reflections, "
                    f"{result.facts_absorbed} absorbed"
                )
            elif result.errors:
                logger.warning(
                    f"[reconciler] synthesis errors for "
                    f"{_mask_id(user_id)}: {result.errors[:3]}"
                )
        except Exception as exc:
            logger.warning(
                f"[reconciler] synthesize_if_ready failed for "
                f"{_mask_id(user_id)}: {exc}"
            )

    async def terminate(self):
        if self._webui_server:
            try:
                await self._webui_server.stop()
            except Exception as e:
                logger.warning(f"Failed to stop WebUI server: {e}")
            self._webui_server = None

        # Stop the auto-promote tick first so it doesn't try to write
        # mid-shutdown. cancel() then await with short timeout — the
        # tick spends most of its time in asyncio.sleep, which yields
        # to CancelledError immediately.
        if self._promote_tick_task and not self._promote_tick_task.done():
            self._promote_tick_task.cancel()
            try:
                await asyncio.wait_for(self._promote_tick_task, timeout=2.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
        self._promote_tick_task = None

        # Drain any in-flight reconciler synthesis tasks alongside the
        # auditor tasks. They share the same auditor LLM client chain
        # so the shutdown deadline (5s) applies to the combined pool.
        if self._reconciler_tasks:
            pending = list(self._reconciler_tasks)
            try:
                await asyncio.wait_for(
                    asyncio.gather(*pending, return_exceptions=True),
                    timeout=5.0,
                )
            except asyncio.TimeoutError:
                for t in pending:
                    if not t.done():
                        t.cancel()

        # Drain any in-flight auditor tasks so we don't try to write to a
        # closed DB after this point. 5s ceiling — auditor itself has 15s
        # but during shutdown we'd rather lose late writes than block exit.
        if self._auditor_tasks:
            pending = list(self._auditor_tasks)
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

                # Phase 3a — every accepted profile write is also a
                # rein signal on that profile's evidence row. Limit
                # / conflict outcomes don't emit a signal because no
                # information was actually persisted. ``force=True``
                # is a user-directive signal (heavier weight); plain
                # set is auto-strength.
                if status in ("set", "updated", "truncated"):
                    target_eid = (
                        self.db.evidence_target_id_for_profile(target_uid, key)
                    )
                    try:
                        self.db.record_evidence_signal(
                            "profile",
                            target_eid,
                            rein_delta=1.0 if force else 0.5,
                            source="user_directive" if force else "auto",
                        )
                    except Exception as exc:
                        # Never propagate evidence write failures into
                        # the user-facing tool result — Phase 3a's
                        # signals are advisory; the primary write
                        # already succeeded.
                        logger.warning(f"evidence signal (profile set) failed: {exc}")

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
                # Phase 3a — fact_hash dedup: if the normalized form of
                # this event matches an existing row for the same user,
                # we bump the existing row's importance + emit a rein
                # signal on its evidence ledger instead of inserting
                # a duplicate. Skipped when:
                #   - flag is off (legacy behaviour preserved)
                #   - text is too short to dedup safely
                # In both skip cases we fall through to the plain
                # save_event path, leaving fact_hash NULL.
                dedup_status: Optional[str] = None
                event_id: Optional[int] = None
                if self._enable_fact_dedup and is_dedup_candidate(value):
                    fhash = fact_hash(value)
                    if fhash:
                        imp = importance_from_text(value)
                        event_id, dedup_status = self.db.save_event_with_dedup(
                            target_uid, value,
                            fact_hash=fhash, importance=imp, tag=tag,
                        )
                        # Emit a rein signal keyed by the FACT, not the
                        # event row, so repeated restatements collapse
                        # onto one ledger entry. We use the fact_hash
                        # itself as the target_id under a synthetic
                        # 'fact' kind — Phase 3b's reflection synthesis
                        # is the natural consumer.
                        try:
                            self.db.record_evidence_signal(
                                "fact",
                                f"{target_uid}::{fhash}",
                                rein_delta=0.5,
                                source="user_fact",
                            )
                        except Exception as exc:
                            logger.warning(
                                f"evidence signal (fact) failed: {exc}"
                            )
                if dedup_status is None:
                    # Legacy path: no fact_hash, plain save.
                    event_id = self.db.save_event(target_uid, value, tag=tag)
                    dedup_status = "inserted"

                self.db.cleanup_old_events(target_uid, keep=self.max_event_keep)
                touched_users.add(target_uid)
                target_tag = "" if target_uid == primary_uid else f" @{target_uid}"
                tag_str = f" #{tag}" if tag else ""
                # Surface dedup outcome in the result so the LLM can
                # tell when its memory_update was a repeat — useful for
                # the model's own self-correction loop.
                dedup_note = ""
                if dedup_status == "deduped":
                    dedup_note = " [已合并到既有事件]"
                results.append(f"event{tag_str}{target_tag}: {value}{dedup_note}")

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
        # can self-check for contradictions next turn. primary_uid is
        # guaranteed != "unknown" by the early return at the top of this
        # function, so only the db existence check is meaningful here.
        summary_suffix = ""
        if self.db:
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
            "传 query 进行关键词/语义搜索事件日志（按相关度排序，BM25 + 可选语义重排）。"
            "群聊场景可传 user_id 指定查询哪个发言者(必须是当前对话中的用户)。"
            "触发例: 用户问 '你记得我什么'、'你知道我的信息吗'、'我的画像'，"
            "或对话上下文显示需要 preference/social 类信息时；"
            "用户问 '上次我说过 X 吗'、'我之前提到 Y 没有' 时传 query=X/Y。"
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
                },
                "query": {
                    "type": "string",
                    "description": "可选: 关键词或短语，命中后按相关度排序返回事件日志"
                },
                "limit": {
                    "type": "integer",
                    "description": "可选: query 模式下返回的最大条数（默认 10，上限 30）"
                }
            },
            "required": []
        }
    )
    async def memory_query(self, event: KiraMessageBatchEvent,
                           category: Optional[str] = None,
                           user_id: Optional[str] = None,
                           query: Optional[str] = None,
                           limit: Optional[int] = None,
                           **_) -> str:
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

        # Phase 2 — query-aware recall path. Only taken when the caller
        # explicitly passes ``query``; legacy callers (no query) keep
        # the v2.0.0 "dump everything" behaviour exactly.
        if query is not None and str(query).strip():
            return await self._recall_query(target, str(query).strip(), limit)

        return self.db.get_all_profiles_formatted(
            target,
            max_events=self.max_events,
            category=category,
        )

    async def _recall_query(self, user_id: str, query: str,
                            limit: Optional[int]) -> str:
        """Run the Phase 2 MemoryRecaller and format results for the LLM.

        Falls back to a one-shot LIKE search through the DB if the
        recaller isn't constructed yet (e.g. called before initialize()
        finished — shouldn't happen in practice but the fallback keeps
        the contract simple).
        """
        # Cap to keep the LLM tool response under a reasonable size.
        try:
            k = int(limit) if limit is not None else 10
        except (TypeError, ValueError):
            k = 10
        k = max(1, min(30, k))

        if self._recaller is not None:
            try:
                hits = await self._recaller.recall(user_id, query, budget=k)
            except Exception as exc:
                logger.warning(f"memory_query recall failed: {exc}")
                hits = []
        else:
            rows = self.db.search_events_fts(user_id, query, limit=k)
            from .recall import RecallCandidate  # local import to avoid cycle at import time
            hits = [
                RecallCandidate(
                    event_id=r[0], summary=r[1], created_at=r[2],
                    tag=r[3], score=float(r[4]),
                    stage_scores={"fts": float(r[4])},
                )
                for r in rows
            ]

        if not hits:
            return f"未在事件日志中找到与 '{query}' 相关的记录。"

        lines = [f"<recall query=\"{self.db._sanitize(query)}\" user=\"{user_id}\">"]
        lines.append(f"\n## 最相关的 {len(hits)} 条事件（按相关度排序）\n")
        for cand in hits:
            tag_s = f" [#{cand.tag}]" if cand.tag else ""
            # ts is ISO, slice the date prefix for compactness
            date_prefix = (cand.created_at or "")[:10]
            lines.append(f"- {date_prefix}{tag_s} {cand.summary}")
        lines.append("</recall>")
        return "\n".join(lines)

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
        # Phase 4 — wipe the anti-repeat corpus too. Otherwise the
        # next turn would still flag this user's vocabulary even
        # though they explicitly asked us to forget them.
        self._anti_repeat.clear(user_id)
        logger.info(f"memory_clear for {_mask_id(user_id)}: {profiles_del} profiles, {events_del} events deleted")
        return f"已清除全部记忆: 删除了 {profiles_del} 条画像和 {events_del} 条事件记录。"

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

        # ── C: Phase 4 anti-repeat hint ─────────────────────────────
        # Only emitted when at least one user has a corpus large
        # enough to clear ``df_threshold``. The hint lists the
        # tokens this user has been over-using and instructs the
        # model to vary phrasing. No-op when the buffer is empty.
        if self._enable_anti_repeat and self.db:
            for uid in self._extract_user_ids(event) or []:
                tokens = self._anti_repeat.overused_tokens(
                    uid,
                    lookback=self._anti_repeat_lookback,
                    df_threshold=self._anti_repeat_df_threshold,
                )
                hint = format_anti_repeat_hint(tokens)
                if hint:
                    req.system_prompt.append(Prompt(
                        hint,
                        name="anti_repeat_hint",
                        source="kiraos",
                    ))
                    # One hint per turn is plenty — multiple users
                    # only happen in group chat, where the primary
                    # speaker dominates style continuity. Append once
                    # for the primary user and stop.
                    break

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
        assistant_reply = (step_result.raw_output or "").strip()

        # Phase 4 — feed the anti-repeat corpus regardless of whether
        # the hint injection is enabled. Keeping the buffer warm lets
        # the operator flip enable_anti_repeat on mid-session without
        # waiting K turns for signal to build up.
        if assistant_reply:
            self._anti_repeat.record(user_id, assistant_reply)

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

        # Phase 3b — opportunistically run reflection synthesis on the
        # same turn. The reconciler does a cheap unabsorbed-fact count
        # first and short-circuits if it's below the threshold, so
        # most turns are a no-op at the SQL layer. Independent task so
        # a slow synthesis LLM call can't delay the auditor's writes.
        if (self._enable_reflection_synthesis
                and self._reconciler is not None):
            synth_task = asyncio.create_task(
                self._maybe_synthesize_reflections(user_id)
            )
            self._reconciler_tasks.add(synth_task)
            synth_task.add_done_callback(self._reconciler_tasks.discard)

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

    def _safe_default_llm_client(self):
        """Return the host's default LLM client or None — never raises.

        Used as the ``llm_client_provider`` for EmbeddingService. The
        plugin host can throw various errors before any LLM is wired
        up (during early initialize, in tests, when the user hasn't
        configured a provider); swallowing them here keeps the
        embedding service in a clean "disabled" state instead of
        propagating into recall calls.
        """
        try:
            return self.ctx.get_default_llm_client()
        except Exception:
            return None

    def _get_reranker_client(self):
        """LLM client for Stage-C reranker (recall.py).

        Priority chain, mirroring the auditor's pattern:
          1. ``memory_reranker_model_uuid`` if set
          2. The auditor client chain (configured / fast / default)

        Returning None opts the recall pipeline out of Stage C without
        any other recovery — the recaller treats None as "skip".
        """
        if self._reranker_model_uuid:
            try:
                client = self.ctx.get_llm_client(
                    model_uuid=self._reranker_model_uuid
                )
                if client is not None:
                    return client
                logger.warning(
                    f"[recall] reranker model '{self._reranker_model_uuid}' "
                    "not available, falling back to auditor chain"
                )
            except Exception as exc:
                logger.warning(f"[recall] reranker model load failed: {exc}")
        return self._get_auditor_client()

    def _get_auditor_client(self):
        """Pick the LLM client to drive the auditor.

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

            # The LLM await above can take up to 15s. If terminate() ran
            # during that window (e.g. plugin disabled mid-flight) it will
            # have set self.db = None. Re-check before writing — without
            # this, upsert_with_limit would raise AttributeError. The outer
            # except Exception would catch it, but a clean early-return
            # avoids spurious traceback noise in the logs.
            if not self.db:
                logger.info(
                    f"[auditor] {_mask_id(user_id)}: db closed during LLM call, "
                    "skipping writes"
                )
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
                    # Mirror the rein-signal emission that
                    # memory_update(op='set') performs at line ~936,
                    # so auditor-written profiles also accumulate
                    # evidence weight and appear on the Phase 5
                    # timeline view. ``source='auditor'`` keeps them
                    # distinguishable from main-LLM and user-directive
                    # writes in WebUI per-signal telemetry.
                    try:
                        self.db.record_evidence_signal(
                            "profile",
                            self.db.evidence_target_id_for_profile(user_id, key),
                            rein_delta=0.3,  # lower than memory_update's 0.5; auditor is a guess
                            source="auditor",
                        )
                    except Exception as exc:
                        logger.warning(f"[auditor] evidence signal failed: {exc}")
                else:
                    skipped += 1
                    # Don't dump ``info`` directly — for status="conflict",
                    # ``upsert_with_limit`` puts the existing user value into
                    # ``info['hint']`` (e.g. "现值'小明' (置信度 0.90) ..."),
                    # and ``f"... {info}"`` would render the whole dict via
                    # ``repr``, persisting that PII to the log file. Same
                    # privacy class as the auditor-text-leak fix in round 7.
                    # We log only metadata: status + a content-hash and length
                    # so repeated conflicts are still correlatable for ops.
                    hint_str = (info.get("hint", "") if isinstance(info, dict) else str(info)) or ""
                    hint_digest = (
                        hashlib.sha256(hint_str.encode("utf-8", errors="replace")).hexdigest()[:8]
                        if hint_str else "—"
                    )
                    logger.info(
                        f"[auditor] {_mask_id(user_id)}: {_mask_id(key)} skipped "
                        f"(status={status}, hint_len={len(hint_str)}, hint_sha8={hint_digest})"
                    )

            logger.info(
                f"[auditor] {_mask_id(user_id)}: extracted {len(extracted)}, "
                f"written {written}, skipped {skipped}"
            )
        except Exception as e:
            logger.warning(f"Hippocampus feed failed for {session_id}: {e}")
