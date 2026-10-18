"""
海马体核心逻辑 — 记忆提取、去重、合并、升维

负责从对话中提取事实 → 去重审查 → 合并同义记忆 → 触发升维反思。
遵循宪章 Agent 行为守则的四条铁律。

去重流程（两级）:
1. SHA-256 内容哈希精确去重（零 LLM 调用）
2. FTS5 语义搜索 + LLM 判断（duplicate/update/new）

语义 ID 生成:
- LLM 从事实内容生成简短的 snake_case slug（如 "hates_css"）
- 回退：从文本前缀 + hash 生成
"""

import asyncio
import ast
import json
import re
import time
from typing import Optional

from core.logging_manager import get_logger
from .toml_tree_store import TomlTreeStore, Memory
from .memory_index import MemoryIndex, normalize_tags
from .llm_adapter import chat_text

logger = get_logger("memory_extractor", "green")

# Default upper bound on every external LLM call from the hippocampus pipeline.
# If a call exceeds this, `asyncio.wait_for` raises TimeoutError which the
# surrounding `except Exception` handlers translate into a safe fallback
# (empty extraction / conservative dedup decision). Tuned for the slowest
# tested provider; overridable per-instance via `MemoryExtractor(llm_chat_
# timeout=...)`, which the plugin wires up to the WebUI `llm_chat_timeout`
# config field.
_DEFAULT_LLM_CHAT_TIMEOUT = 30.0

# self-awareness 输出的常见列表 / markdown 前缀。LLM 喜欢把 1-2 条洞察自动
# 加上 `- 我...` / `1. 我...` / `* **我...**` 这种装饰，纯 `startswith("我")`
# 会把它们全杀掉。先剥掉这些前缀再做"以'我'开头"的语义检查。
_LIST_PREFIX_RE = re.compile(r"^[\s\-\*\+\d\.\)、•>#]+\s*")

# Backstop cap on a merged fact/reflection/profile-summary line. The merge
# and compaction prompts already ask for short output, but a model that
# ignores instructions (or the no-LLM fallback) must not be able to grow a
# memory without bound: every later dedup/merge prompt re-sends that text.
_MERGE_MAX_CHARS = 200

# Cap on the number of tags after merge. Each merge adds the new tags to
# the old set; without a limit, a memory can accumulate 100+ tags over many
# updates. Most memories need far fewer.
_MERGE_MAX_TAGS = 20

# Profile compaction: default cap on compacted profile facts, and the tags
# the LLM is asked to sort facts into. Mirrors kira_plugin_hippocampus_memory.
_PROFILE_COMPACT_MAX_FACTS = 12
_PROFILE_FACT_TAGS = ("身份", "性格", "技能兴趣", "互动习惯", "关系", "其他")


def _merge_tags(existing_tags, new_tags) -> list[str]:
    """Union of tags, capped at ``_MERGE_MAX_TAGS``.

    New tags win the budget because they describe the freshest content of
    the merged text; old tags fill whatever slots are left. A memory that
    already exceeds the cap (written by an older version) is trimmed on its
    next merge.
    """
    merged: list[str] = []
    seen: set[str] = set()
    for tag in [*normalize_tags(new_tags), *normalize_tags(existing_tags)]:
        if tag not in seen:
            seen.add(tag)
            merged.append(tag)
        if len(merged) >= _MERGE_MAX_TAGS:
            break
    return sorted(merged)


def _clip_merged(text: str) -> str:
    text = (text or "").strip()
    if len(text) > _MERGE_MAX_CHARS:
        text = text[: _MERGE_MAX_CHARS - 1].rstrip() + "…"
    return text


def _merge_fallback(existing_text: str, new_text: str) -> str:
    """Bounded no-LLM merge that always keeps the newer information.

    The new text is kept whole (clipped to the cap); the existing text is
    trimmed to whatever budget is left, so repeated fallbacks can't grow a
    memory past ``_MERGE_MAX_CHARS``.
    """
    existing_text = (existing_text or "").strip()
    new_text = _clip_merged(new_text)
    if not existing_text:
        return new_text
    if not new_text:
        return _clip_merged(existing_text)

    separator = "；"
    if len(existing_text) + len(separator) + len(new_text) <= _MERGE_MAX_CHARS:
        return f"{existing_text}{separator}{new_text}"

    existing_budget = _MERGE_MAX_CHARS - len(separator) - len(new_text)
    if existing_budget <= 0:
        return new_text
    if len(existing_text) > existing_budget:
        if existing_budget == 1:
            existing_text = "…"
        else:
            existing_text = existing_text[: existing_budget - 1].rstrip() + "…"
    return f"{existing_text}{separator}{new_text}"


class MemoryExtractor:
    """海马体：事实提取 → 去重 → 合并 → 升维"""

    # 单次提取 prompt 里 conversation_text 的字符上限。8000 chars ≈ 4–6k
    # tokens，留出足够空间给系统 prompt + JSON 输出格式 + 个人 profile
    # 上下文。超出部分按"最近优先"截断——海马体只关心新对话，旧上下文
    # 已经走过前几轮提取，再塞一次只会膨胀 cost/timeout 而无新增信号。
    MAX_CONVERSATION_CHARS = 8000

    def __init__(
        self,
        tree_store: TomlTreeStore,
        extraction_client=None,
        reflection_client=None,
        *,
        llm_client=None,  # Backward compatibility
        llm_chat_timeout: float = _DEFAULT_LLM_CHAT_TIMEOUT,
    ):
        self.tree_store = tree_store
        self.index: MemoryIndex = tree_store.index
        # 每条 LLM 调用的超时；超过即被当成失败、走空提取兜底（不会丢 chunks，
        # 上游的 hippocampus_process 会 re-buffer 回 pending 等下次触发）。
        self.llm_chat_timeout: float = float(llm_chat_timeout)

        # Backward compatibility: the pre-split signature was
        # `MemoryExtractor(tree_store, llm_client)`, where one client served both
        # roles. A lone second argument (positional or via the llm_client kwarg)
        # keeps that meaning — otherwise reflection would silently go unwired.
        if llm_client is not None:
            extraction_client = extraction_client or llm_client
            reflection_client = reflection_client or llm_client
        elif extraction_client is not None and reflection_client is None:
            reflection_client = extraction_client

        self._extraction_client = extraction_client  # for extract_*, _check_conflict, merge_facts
        self._reflection_client = reflection_client  # for generate_reflections, profile compact (future)

        # 升维阈值：facts 积累达到此数量时触发反思
        self.reflection_threshold = 5
        # 升维输入上限：单条 LLM 调用最多塞这么多 fact 进 prompt。
        # 不设上限会让 prompt 按实体规模线性膨胀，成本/超时率失控。
        self.reflection_input_cap = 50

    @classmethod
    def _truncate_conversation(cls, text: str) -> str:
        """把 conversation_text 截到 MAX_CONVERSATION_CHARS（保留尾部）。"""
        if not text:
            return ""
        if len(text) <= cls.MAX_CONVERSATION_CHARS:
            return text
        # 取末尾部分（最近内容），并在前面加一个明显的截断标记给 LLM 看，
        # 避免模型把开头不完整的句子当成完整事实。
        return "[…earlier conversation truncated…]\n" + text[-cls.MAX_CONVERSATION_CHARS:]

    def set_extraction_client(self, client):
        """Set the LLM client for extraction, dedup, and merge operations."""
        self._extraction_client = client

    def set_reflection_client(self, client):
        """Set the LLM client for reflection generation and profile compaction."""
        self._reflection_client = client

    def set_llm_client(self, client):
        """Backward compatibility: set both extraction and reflection to the same client."""
        self._extraction_client = client
        self._reflection_client = client

    def set_fast_llm_client(self, client):
        """Backward compatibility: set extraction client (fast operations)."""
        self._extraction_client = client

    # ==========================================
    # 事实提取（双路径）
    # ==========================================

    async def extract_personal_facts(self, conversation_text: str) -> list[dict]:
        """从对话中提取个人事实（用户级）

        专注于每位用户的偏好、身份、经历、观点、习惯等。
        结果将路由到各用户的 entity 目录下。

        Returns:
            [{"content": "...", "importance": 7, "tags": [...],
              "speaker_id": "12345", "subject": "昵称", "semantic_id": "..."}, ...]
        """
        if not self._extraction_client:
            return []
        conversation_text = self._truncate_conversation(conversation_text)

        prompt = f"""分析以下对话片段，提取每位用户的**个人事实**。忽略寒暄和无意义内容。
对话中每位用户的格式为 "昵称(ID): 内容"，请注意区分不同用户。

只关注个人层面的信息，包括：
- 用户的偏好、喜好、厌恶
- 身份信息（职业、年龄、所在地等）
- 个人经历、故事
- 观点、立场
- 习惯、性格特征

对话:
{conversation_text}

请以 JSON 数组格式输出，每条事实包含：
- "speaker_id": 该事实所属用户的 ID（从对话中括号内提取，如 "12345"）
- "subject": 该用户的昵称
- "content": 事实描述，用该用户昵称作主语，写成完整陈述句。例如：✅ "小明喜欢用Python" ✅ "阿花是一名大三学生" ❌ "该用户喜欢Python"（禁止使用"该用户"）
- "importance": 重要性评分(1-10)
- "tags": 相关标签数组
- "semantic_id": 简短 snake_case 标识符（如 "xiaoming_likes_python"）

**严禁使用"该用户""该成员""此人"等模糊代词，必须用具体昵称。**

只输出 JSON 数组，不要有其他内容。如果没有值得记录的个人事实，输出空数组 []。"""

        try:
            resp = await asyncio.wait_for(
                chat_text(self._extraction_client, prompt),
                timeout=self.llm_chat_timeout
            )
            if resp:
                return self._parse_json_array(resp)
        except Exception as e:
            if isinstance(e, asyncio.TimeoutError):
                logger.warning("Personal fact extraction timed out after %gs; LLM provider may be slow or rate-limited", self.llm_chat_timeout)
            else:
                logger.error("Personal fact extraction error: %s: %s", type(e).__name__, e)
        return []

    async def extract_group_facts(self, conversation_text: str) -> list[dict]:
        """从对话中提取群组事实（群级）

        专注于群聊整体的信息：氛围、话题、成员关系、群体特征。
        结果将路由到群组 entity 目录下。

        Returns:
            [{"content": "...", "importance": 7, "tags": [...],
              "subject": "group", "semantic_id": "..."}, ...]
        """
        if not self._extraction_client:
            return []
        conversation_text = self._truncate_conversation(conversation_text)

        prompt = f"""分析以下群聊对话片段，提取**群组级别**的信息。忽略寒暄和无意义内容。
对话中每位用户的格式为 "昵称(ID): 内容"。

只关注群聊层面的信息，包括：
- 群聊的常见话题和讨论方向
- 群体氛围、文化特征
- 成员之间的互动关系和社交动态（如"小明和阿花经常互怼"）
- 群内的共识、群规、惯例
- 群内事件（如群友组织活动、群聊里发生的趣事）

对话:
{conversation_text}

请以 JSON 数组格式输出，每条事实包含：
- "speaker_id": 留空 ""
- "subject": "group"
- "content": 事实描述，写成关于群聊的完整陈述句。涉及具体成员时必须用昵称，例如：✅ "群里最近在讨论AI绘画" ✅ "小明和阿花经常在群里互怼" ✅ "群友们普遍偏好深夜聊天" ❌ "该用户经常发言"（禁止使用"该用户"，且这不是群级信息）
- "importance": 重要性评分(1-10)
- "tags": 相关标签数组
- "semantic_id": 简短 snake_case 标识符（如 "group_discusses_ai_art"）

**严禁使用"该用户""该成员""此人"等模糊代词，涉及具体人时用昵称。**
**不要提取个人偏好/身份等个人事实，那些由另一个流程处理。**

只输出 JSON 数组，不要有其他内容。如果没有值得记录的群组事实，输出空数组 []。"""

        try:
            resp = await asyncio.wait_for(
                chat_text(self._extraction_client, prompt),
                timeout=self.llm_chat_timeout
            )
            if resp:
                return self._parse_json_array(resp)
        except Exception as e:
            if isinstance(e, asyncio.TimeoutError):
                logger.warning("Group fact extraction timed out after %gs; LLM provider may be slow or rate-limited", self.llm_chat_timeout)
            else:
                logger.error("Group fact extraction error: %s: %s", type(e).__name__, e)
        return []

    async def extract_facts(self, conversation_text: str) -> list[dict]:
        """从对话中提取事实（私聊兼容接口）

        私聊场景只有一个用户，不需要双路径，走单次提取即可。
        """
        if not self._extraction_client:
            return []
        conversation_text = self._truncate_conversation(conversation_text)

        prompt = f"""分析以下对话片段，提取关键事实。忽略寒暄和无意义内容。
对话中用户的格式为 "昵称(ID): 内容"。

对话:
{conversation_text}

请以 JSON 数组格式输出，每条事实包含：
- "speaker_id": 该事实所属用户的 ID（从对话中括号内提取，如 "12345"）
- "subject": 该用户的昵称
- "content": 事实描述，用昵称作主语，写成完整陈述句。例如：✅ "小明喜欢吃辣" ❌ "该用户喜欢吃辣"
- "importance": 重要性评分(1-10)
- "tags": 相关标签数组
- "semantic_id": 简短 snake_case 标识符（如 "xiaoming_likes_spicy"）

**严禁使用"该用户"等模糊代词，必须用具体昵称。**

只输出 JSON 数组，不要有其他内容。如果没有值得记录的事实，输出空数组 []。"""

        try:
            resp = await asyncio.wait_for(
                chat_text(self._extraction_client, prompt),
                timeout=self.llm_chat_timeout
            )
            if resp:
                return self._parse_json_array(resp)
        except Exception as e:
            if isinstance(e, asyncio.TimeoutError):
                logger.warning("Fact extraction timed out after %gs; LLM provider may be slow or rate-limited", self.llm_chat_timeout)
            else:
                logger.error("Fact extraction error: %s: %s", type(e).__name__, e)
        return []

    # ==========================================
    # 自我觉察提取（Phase 1: 只存不读）
    # ==========================================

    async def extract_self_awareness(
        self, conversation_text: str, ai_response_text: str = ""
    ) -> list[str]:
        """从对话中提取 AI 关于自身行为的觉察

        Phase 1 只存不读：觉察写入 global/self/facts/，不影响召回。
        大部分对话不应产出觉察（返回空列表）。只有当 AI 在这次互动中
        表现出明显的行为模式时才记录。

        Args:
            conversation_text: 本轮对话全文
            ai_response_text: AI 在这轮对话中的回复文本（可选）

        Returns:
            觉察文本列表（通常 0-2 条，大部分情况为空）
        """
        if not self._extraction_client:
            return []
        conversation_text = self._truncate_conversation(conversation_text)

        response_section = ""
        if ai_response_text:
            response_section = f"\n\n你的回复:\n{ai_response_text}"

        prompt = f"""你刚刚参与了一段对话。请回顾这次互动，思考你自己在这次对话中的**行为表现**。

对话内容:
{conversation_text}{response_section}

请思考：
- 你的回复风格有什么特点？（比如偏啰嗦/偏简短、语气偏冷/偏热情）
- 你处理这类话题/这类用户时有什么倾向？
- 有没有什么做得不好的地方，或者做得特别好的地方？
- 你注意到自己的什么习惯或模式？

**输出要求**：
- 只关注你自己的行为模式，不要总结对话内容
- 每条觉察必须以"我"开头（例如："我在回答技术问题时倾向于给出过于详细的解释"）
- 只输出有价值的觉察，不要为了输出而输出
- 如果这次对话没有值得记录的行为觉察，直接输出 NONE
- 如果有，每条一行，最多2条

直接输出觉察内容或 NONE，不要有其他内容。"""

        try:
            resp = await asyncio.wait_for(
                chat_text(self._extraction_client, prompt),
                timeout=self.llm_chat_timeout
            )
            if resp:
                text = resp.strip()
                if text.upper() == "NONE" or not text:
                    return []
                # 先去掉项目符号 / markdown 前缀和 emphasis 字符，再做"我"开头的语义判断。
                # 多数 LLM 会输出 `- 我...`、`1. 我...`、`* **我...**` 这种装饰；
                # 直接 startswith("我") 会把它们一刀切，self-awareness 链路常年空手。
                stripped_lines = []
                for line in text.split("\n"):
                    s = line.strip()
                    if not s or s.upper() == "NONE":
                        continue
                    s = _LIST_PREFIX_RE.sub("", s)
                    # 再剥两侧 markdown emphasis（**、_、`）
                    s = s.strip("*_`").strip()
                    if s:
                        stripped_lines.append(s)
                insights = [
                    s for s in stripped_lines
                    if s.startswith("我") and 5 < len(s) < 200
                ]
                return insights[:2]  # 最多 2 条
        except Exception as e:
            if isinstance(e, asyncio.TimeoutError):
                logger.warning("Self-awareness extraction timed out after %gs; LLM provider may be slow or rate-limited", self.llm_chat_timeout)
            else:
                logger.error("Self-awareness extraction error: %s: %s", type(e).__name__, e)
        return []

    # ==========================================
    # 语义 ID 生成
    # ==========================================

    async def generate_semantic_id(self, content: str) -> str:
        """让 LLM 生成语义化 slug ID

        回退策略：文本前缀 + hash
        """
        if not self._extraction_client:
            return ""

        prompt = f"""为以下记忆内容生成一个简短的 snake_case 文件名标识符（英文，无空格，不超过 30 字符）。
例如：hates_css, loves_python, pet_cat_xiaoju, prefers_dark_mode

内容: {content}

只输出标识符，不要有其他内容。"""

        try:
            resp = await asyncio.wait_for(
                chat_text(self._extraction_client, prompt),
                timeout=self.llm_chat_timeout
            )
            if resp:
                slug = resp.strip().lower()
                # 清理非法字符
                slug = re.sub(r"[^a-z0-9_]", "_", slug)
                slug = re.sub(r"_+", "_", slug).strip("_")
                if slug and len(slug) <= 40:
                    return slug
        except Exception as e:
            if isinstance(e, asyncio.TimeoutError):
                logger.warning("Semantic ID generation timed out after %gs; LLM provider may be slow or rate-limited", self.llm_chat_timeout)
            else:
                logger.debug(f"Semantic ID generation failed: {type(e).__name__}: {e}")
        return ""

    # ==========================================
    # 去重审查（宪章铁律 #1）
    # ==========================================

    async def deduplicate(
        self,
        new_content: str,
        entity_id: str,
        entity_type: str = "user",
        folder: str = "facts",
        base_dir: str = "",
    ) -> tuple[str, Optional[Memory]]:
        """两级去重：SHA-256 精确匹配 → FTS5 语义搜索 + LLM 判断

        Returns:
            (decision, matched_memory)
            decision: "duplicate" | "update" | "new"
            matched_memory: 匹配到的旧记忆（仅 duplicate/update 时非 None）
        """
        # === 第一级：SHA-256 精确去重（零 LLM 调用） ===
        # 每条 fact 都会进这条路径——一次同步 SQLite 调用就把整条 async 海马体
        # 流水线卡住，跟前面统一用 `to_thread` / `_DEFAULT_LLM_CHAT_TIMEOUT` 控边界的
        # 思路相违。offload 到线程池保持一致。
        content_hash = MemoryIndex.content_hash(new_content)
        exact_match = await asyncio.to_thread(
            self.index.find_by_hash,
            content_hash, entity_id, entity_type, folder, base_dir,
        )
        if exact_match:
            logger.debug(f"Exact hash match: {new_content[:50]}...")
            return "duplicate", None

        # === 第二级：FTS5 语义搜索 + LLM 判断（多候选） ===
        existing = await self.tree_store.search(
            query=new_content,
            entity_id=entity_id,
            entity_type=entity_type,
            folder=folder,
            k=3,
            update_access=False,
        )

        if not existing:
            return "new", None

        # 逐条检查，命中即返回（按相似度排序，最相似的先检查）
        for candidate in existing:
            decision = await self._check_conflict(new_content, candidate.text)
            if decision in ("duplicate", "update"):
                return decision, candidate

        return "new", None

    async def _check_conflict(self, new_content: str, existing_content: str) -> str:
        """用 LLM 判断新旧记忆的关系（使用快速模型）"""
        client = self._extraction_client
        if not client:
            return "new"

        prompt = f"""比较以下两条信息，判断它们的关系：

已有信息: {existing_content}
新信息: {new_content}

只输出以下三个选项之一：
- "duplicate"：新信息与已有信息基本相同，无需记录
- "update"：新信息是对已有信息的更新或补充，需要合并
- "new"：新信息与已有信息无关，是全新信息

只输出选项文本，不要有其他内容。"""

        try:
            resp = await asyncio.wait_for(
                chat_text(client, prompt),
                timeout=self.llm_chat_timeout
            )
            if resp:
                result = resp.strip().strip('"').lower()
                if result in ("duplicate", "update", "new"):
                    return result
        except Exception as e:
            if isinstance(e, asyncio.TimeoutError):
                logger.warning("Conflict check timed out after %gs; LLM provider may be slow or rate-limited", self.llm_chat_timeout)
            else:
                logger.error("Conflict check error: %s: %s", type(e).__name__, e)
        return "new"

    # ==========================================
    # 合并
    # ==========================================

    async def merge_facts(self, existing_text: str, new_text: str) -> str:
        """LLM 合并两条事实为一条（使用快速模型）

        合并结果必须是一条短陈述：旧版提示词要求「保留所有有用信息」，
        泛化记忆（如「群聊氛围友善」）会在每次 update 时不断变长，而之后
        每次去重/合并都会把这段长文本再塞进 prompt。提示词要求精简，代码
        层再用 ``_clip_merged`` 硬截断兜底；LLM 不可用/失败时走有长度预算
        的 ``_merge_fallback``，而不是无上限拼接。
        """
        client = self._extraction_client
        if not client:
            return _merge_fallback(existing_text, new_text)

        prompt = f"""将以下两条信息合并为一条更简短的陈述：

已有信息: {existing_text}
新信息: {new_text}

规则：
- 两者冲突时以新信息为准
- 只保留关键事实，不要罗列细节，不要重复同义内容
- 合并结果控制在 40 字以内

直接输出合并后的结果，不要有其他内容。"""

        try:
            resp = await asyncio.wait_for(
                chat_text(client, prompt),
                timeout=self.llm_chat_timeout
            )
            merged = (resp or "").strip()
            if merged:
                return _clip_merged(merged)
        except Exception as e:
            if isinstance(e, asyncio.TimeoutError):
                logger.warning("Merge facts timed out after %gs; LLM provider may be slow or rate-limited", self.llm_chat_timeout)
            else:
                logger.error("Merge facts error: %s: %s", type(e).__name__, e)
        return _merge_fallback(existing_text, new_text)

    # ==========================================
    # 去重并存储（完整流程）
    # ==========================================

    async def deduplicate_and_store(
        self,
        fact: dict,
        entity_id: str,
        entity_type: str = "user",
    ):
        """铁律 #1 完整实现：去重 → 合并/新增

        Args:
            fact: {"content": "...", "importance": 7, "tags": [...], "semantic_id": "..."}
        """
        content = fact.get("content", "")
        importance = fact.get("importance", 5)
        tags = fact.get("tags", [])
        semantic_id = fact.get("semantic_id", "")

        if not content:
            return

        decision, matched = await self.deduplicate(
            content, entity_id, entity_type, "facts"
        )

        if decision == "duplicate":
            logger.debug(f"Duplicate memory skipped: {content[:50]}...")
            return

        if decision == "update" and matched:
            # 合并后更新旧记忆
            merged_text = await self.merge_facts(matched.text, content)
            matched.text = merged_text
            matched.importance = max(importance, matched.importance)
            matched.meta["last_accessed"] = time.time()

            # 合并 tags —— 两边都先规整再做集合运算。`set()` 吃到 TOML 里
            # 手工写进去的 dict/list 会抛 unhashable TypeError，而这个循环
            # 外面只有 hippocampus_process 的一个大 try，异常会把本批剩余
            # facts 连同升维、画像更新一起吞掉，不是只丢一个 tag。
            # 上限见 `_MERGE_MAX_TAGS`：新 tag 优先占位。
            matched.tags = _merge_tags(matched.tags, tags)

            if await self.tree_store.update_memory(matched):
                logger.info(f"Memory merged: id={matched.id}")
            else:
                logger.warning(f"Failed to merge memory {matched.id}")
            return

        # 全新事实 → 写入
        # 尝试获取语义 ID
        if not semantic_id:
            semantic_id = await self.generate_semantic_id(content)

        await self.tree_store.add_memory(
            content_text=content,
            memory_type="fact",
            importance=importance,
            tags=tags,
            semantic_id=semantic_id,
            entity_id=entity_id,
            entity_type=entity_type,
            folder="facts",
        )
        logger.info(f"New fact stored for {entity_type}:{entity_id}")

    # ==========================================
    # 信息升维（宪章铁律 #2）
    # ==========================================

    async def check_elevation_trigger(
        self,
        entity_id: str,
        entity_type: str = "user",
    ) -> bool:
        """检查 facts 是否积累到升维阈值"""
        facts = await self.tree_store.get_all_memories(
            entity_id=entity_id, entity_type=entity_type, folder="facts"
        )
        return len(facts) >= self.reflection_threshold

    async def generate_reflections(
        self,
        entity_id: str,
        entity_type: str = "user",
    ) -> list[str]:
        """从 facts 群提炼 reflections（升维），并归档被吸收的 facts

        Returns:
            生成的 reflection 文本列表
        """
        if not self._reflection_client:
            return []

        facts = await self.tree_store.get_all_memories(
            entity_id=entity_id, entity_type=entity_type, folder="facts"
        )
        if len(facts) < self.reflection_threshold:
            return []

        # 限制升维输入窗口：按 importance + last_accessed 降序取 Top-50。
        # 不设上限的话整批事实会按实体规模线性膨胀进 prompt——成本、超时
        # 率和失败率都会跟着涨，最后海马体只会频繁走空结果降级。
        facts = sorted(
            facts,
            key=lambda f: (f.importance, f.last_accessed),
            reverse=True,
        )[: self.reflection_input_cap]

        facts_text = "\n".join(
            f"{i + 1}. {f.text}" for i, f in enumerate(facts)
        )

        if entity_type == "group":
            prompt = f"""基于以下关于这个群聊的事实，你能推断出什么更高层面的洞察？
比如群体性格、社交动态、群文化特征等。涉及具体成员时用昵称，不要说"该用户"。

事实:
{facts_text}

请输出 1-3 条简洁的洞察，每条一行，不需要编号。只输出洞察内容，不要有其他内容。"""
        else:
            prompt = f"""基于以下关于这位用户的事实，你能推断出什么更高层面的洞察？
比如性格特征、兴趣偏好的模式、生活方式等。用该用户的昵称作主语，不要说"该用户"。

事实:
{facts_text}

请输出 1-3 条简洁的洞察，每条一行，不需要编号。只输出洞察内容，不要有其他内容。"""

        generated = []
        try:
            resp = await asyncio.wait_for(
                chat_text(self._reflection_client, prompt),
                timeout=self.llm_chat_timeout
            )
            if not resp:
                return []

            insights = [
                line.strip()
                for line in resp.strip().split("\n")
                if line.strip()
            ][:3]  # prompt 已经要求 1-3 条，超出的多余行会触发额外 search/merge/add
            # 的成本和噪声——硬截到 3 条与 prompt 契约对齐。

            for insight in insights:
                # 去重检查：是否已有相似 reflection
                existing = await self.tree_store.search(
                    query=insight,
                    entity_id=entity_id,
                    entity_type=entity_type,
                    folder="reflections",
                    k=1,
                    update_access=False,
                )
                if existing:
                    # FTS almost always returns *some* reflection once an
                    # entity has a few, so merging unconditionally funnels
                    # every new insight into the same handful of entries.
                    # Run the same duplicate/update/new check facts use.
                    decision = await self._check_conflict(insight, existing[0].text)
                    if decision == "duplicate":
                        logger.debug(f"Duplicate reflection skipped: {insight[:50]}...")
                        continue
                    if decision == "update":
                        merged = await self.merge_facts(existing[0].text, insight)
                        existing[0].text = merged
                        existing[0].meta["last_accessed"] = time.time()
                        await self.tree_store.update_memory(existing[0])
                        logger.debug(f"Reflection merged with existing: {insight[:50]}...")
                        continue
                    # "new" → fall through and store as a separate reflection.

                # 生成语义 ID
                sem_id = await self.generate_semantic_id(insight)

                await self.tree_store.add_memory(
                    content_text=insight,
                    memory_type="reflection",
                    importance=7,
                    semantic_id=sem_id,
                    entity_id=entity_id,
                    entity_type=entity_type,
                    folder="reflections",
                )
                generated.append(insight)
                logger.info(f"Reflection stored for {entity_type}:{entity_id}")

            # 不再自动批量归档低 importance facts。原逻辑只要 reflection
            # 非空就把所有 importance <= 4 的 fact 全归档，但代码并没有证明
            # 这些 fact 真的被当前 reflection 吸收——无关但仍有价值的原始
            # 细节会被一起删走，召回路径只剩抽象结论。要真做吸收追踪，需要
            # 在 prompt 输出里显式列出"被吸收 fact 的 id 集合"，再按 id
            # 精确归档；现版本里没有这条信息，保守的选择是停掉自动归档，
            # 让 `memory_decay` 的衰减机制按 importance + last_accessed
            # 自然清理低价值条目。
            if generated:
                logger.debug(
                    f"Generated {len(generated)} reflection(s) for "
                    f"{entity_type}:{entity_id}; skipping auto-archive of "
                    "low-importance facts (no absorption tracking yet)"
                )

        except Exception as e:
            if isinstance(e, asyncio.TimeoutError):
                logger.warning("Reflection generation timed out after %gs; LLM provider may be slow or rate-limited", self.llm_chat_timeout)
            else:
                logger.error("Reflection generation error: %s: %s", type(e).__name__, e)

        return generated

    # ==========================================
    # 画像压实（画像去重精简）
    # ==========================================

    async def summarize_profile_facts(
        self,
        facts: list[str],
        traits: Optional[list[str]] = None,
        max_facts: int = _PROFILE_COMPACT_MAX_FACTS,
    ) -> list[str]:
        """把冗长/重复的画像 facts 压实为带 "[标签]" 前缀的规范短句列表。

        用于修复已经膨胀的存量画像（例如同一个习惯被反复用不同话描述好几遍）。
        解析失败或空输出时返回 ``[]``——调用方（``memory_manager.compact_
        profile``）必须据此保留原值，绝不能用空列表覆盖已有画像。

        ``max_facts`` 由调用方按压实阈值动态传入（而非固定用模块常量），否则
        压实后 facts 数量仍可能 ≥ 阈值，导致下一次写入立刻再次触发压实
        （re-compaction loop）。
        """
        if not self._reflection_client or not facts:
            return []
        max_facts = max(1, max_facts)

        facts_text = "\n".join(f"- {f}" for f in facts)
        traits_text = f"\n已有特征标签: {', '.join(traits)}" if traits else ""
        tags = "、".join(_PROFILE_FACT_TAGS)

        prompt = f"""以下是关于同一个人的一份画像事实列表，其中可能有重复、同义
或过于细碎的内容（比如同一个习惯被反复用不同话描述了好几次）。请把它们去重、
合并、精简为规范的短句列表。

规则：
- 每条不超过 30 字，一条只说一件事
- 同义/重复内容只保留一条最准确的表述，不要罗列发生了几次
- 每条前面加一个 "[标签]" 前缀，标签从以下几类中选：{tags}
- 按重要性排序，最多输出 {max_facts} 条
- 不要编造原始列表里没有的信息

原始事实列表:
{facts_text}{traits_text}

只输出结果，每行一条，格式为 "[标签] 内容"，不要有编号、不要有其他说明文字。"""

        try:
            text = await asyncio.wait_for(
                chat_text(self._reflection_client, prompt),
                timeout=self.llm_chat_timeout,
            )
            text = (text or "").strip()
            if not text:
                return []
            lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
            cleaned = []
            for ln in lines:
                # 容错去掉模型可能加的项目符号/编号前缀。
                ln = re.sub(r"^[-*\d.\s]+(?=\[)", "", ln).strip()
                if not ln:
                    continue
                # 只接受「[允许的标签] 非空内容」这种规范格式；拒绝语、裸 JSON、
                # 未打标签的文本等一律视为解析失败整体返回 []，而不是自动包装
                # 成 "[其他]" 事实去覆盖原始画像（untagged LLM output accepted）。
                # "]" 后的空格可有可无（中文模型常写 "[身份]小明…"），统一规范成
                # "[标签] 内容"。
                match = re.fullmatch(r"\[([^\]\n]+)\]\s*(\S.*)", ln)
                if match is None or match.group(1) not in _PROFILE_FACT_TAGS:
                    return []
                cleaned.append(_clip_merged(f"[{match.group(1)}] {match.group(2)}"))
            if not cleaned:
                return []
            return cleaned[:max_facts]
        except Exception as e:
            if isinstance(e, asyncio.TimeoutError):
                logger.warning("Profile summarize timed out after %gs", self.llm_chat_timeout)
            else:
                logger.error(f"Profile compaction summarize error: {e}")
            return []

    # ==========================================
    # 工具方法
    # ==========================================

    @staticmethod
    def _parse_json_array(text: str) -> list[dict]:
        """健壮地解析 LLM 输出的 JSON 数组"""
        text = text.strip()

        # 去除 markdown code fence
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()

        # 提取第一个 JSON 数组
        start = text.find("[")
        end = text.rfind("]")
        if start != -1 and end != -1 and end > start:
            text = text[start: end + 1]

        # 多次尝试解析
        for attempt in range(3):
            try:
                if attempt == 1:
                    # 移除尾随逗号
                    text = re.sub(r",\s*([}\]])", r"\1", text)
                if attempt == 2:
                    # 回退到 ast.literal_eval
                    obj = ast.literal_eval(text)
                    result = json.loads(json.dumps(obj))
                    if isinstance(result, list):
                        return _clean_facts(result)
                    return []

                result = json.loads(text)
                if isinstance(result, list):
                    return _clean_facts(result)
                return []
            except (json.JSONDecodeError, ValueError, SyntaxError):
                continue

        return []


def _clean_facts(facts: list) -> list[dict]:
    """清理和标准化事实列表"""
    cleaned = []
    for f in facts:
        if not isinstance(f, dict) or "content" not in f:
            continue
        # content 必须是非空字符串。模型偶尔会输出 `{"content": {}}`、数字、
        # 纯空白这种垃圾值——后面 content_hash / 去重 / 写盘任何一环都会炸，
        # 在源头收口跳过更安全。
        raw_content = f.get("content")
        if not isinstance(raw_content, str):
            continue
        content = raw_content.strip()
        if not content:
            continue
        f["content"] = content
        # 标准化 importance
        raw_imp = f.get("importance")
        if raw_imp is None or raw_imp == "":
            f["importance"] = 5
        else:
            try:
                f["importance"] = max(1, min(10, int(float(raw_imp))))
            except (ValueError, TypeError):
                f["importance"] = 5
        # 确保 tags 是 list[str]，并清掉空白 / 不可哈希值。下游 dedup 路径
        # 会跑 `set(matched.tags).update(tags)`——一个 dict 或 list 元素就
        # 直接 `TypeError: unhashable type` 把整条流水线打断。
        raw_tags = f.get("tags")
        if not isinstance(raw_tags, list):
            f["tags"] = []
        else:
            cleaned_tags: list[str] = []
            seen: set[str] = set()
            for tag in raw_tags:
                if not isinstance(tag, (str, int, float, bool)):
                    continue
                s = str(tag).strip()
                if not s or s in seen:
                    continue
                cleaned_tags.append(s)
                seen.add(s)
            f["tags"] = cleaned_tags
        # 清理 semantic_id
        sem_id = f.get("semantic_id", "")
        if sem_id:
            sem_id = re.sub(r"[^a-z0-9_]", "_", sem_id.lower())
            sem_id = re.sub(r"_+", "_", sem_id).strip("_")
            f["semantic_id"] = sem_id
        cleaned.append(f)
    return cleaned
