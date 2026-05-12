"""
Hippocampus core — fact extraction, dedup, merge, elevation.

Pipeline:
  1. SHA-256 exact dedup (zero LLM calls)
  2. FTS5 semantic search + LLM judgement (duplicate / update / new)
  3. Merge or write
  4. When facts accumulate past a threshold, elevate them into reflections

This is the KiraAI-adapted port of KiraAI-lightning's MemoryExtractor. The
LLM call surface is wrapped in ``_chat(prompt)`` so the rest of the class
stays close to the lightning original.
"""

import ast
import json
import re
import time
from typing import Optional

from core.logging_manager import get_logger

from .toml_tree_store import TomlTreeStore, Memory
from .memory_index import MemoryIndex

# ``LLMRequest`` and ``Prompt`` are imported lazily inside ``_chat`` to avoid
# pulling the heavy ``core.provider`` graph at module-load time. KiraAI's
# provider/agent/chat/adapter modules form a circular import that only
# resolves through ``main.py``'s startup ordering; importing eagerly here
# breaks standalone test runs.

logger = get_logger("kiraos_memory_extractor", "green")


class MemoryExtractor:
    """Hippocampus: extract → dedupe → merge → elevate."""

    def __init__(self, tree_store: TomlTreeStore, llm_client=None):
        self.tree_store = tree_store
        self.index: MemoryIndex = tree_store.index
        self._llm_client = llm_client
        self._fast_llm_client = None

        # Reflection elevation threshold: number of facts that triggers it.
        self.reflection_threshold = 5

    def set_llm_client(self, llm_client):
        self._llm_client = llm_client

    def set_fast_llm_client(self, fast_llm_client):
        """Lightweight model client for dedup/merge tasks (falls back to main)."""
        self._fast_llm_client = fast_llm_client

    @property
    def _fast_or_default(self):
        return self._fast_llm_client or self._llm_client

    async def _chat(self, prompt: str, *, fast: bool = False) -> str:
        """Send a prompt to the configured LLM and return the text response.

        Adapter around KiraAI's ``LLMRequest`` API so the rest of the class can
        keep lightning's simple prompt-in / text-out shape. Returns empty string
        on missing client or error — callers must handle "" as no-op.
        """
        client = self._fast_or_default if fast else self._llm_client
        if client is None:
            return ""
        from core.provider.llm_model import LLMRequest
        from core.prompt_manager import Prompt
        try:
            req = LLMRequest(
                user_prompt=[Prompt(prompt, name="hippocampus", source="kiraos")],
            )
            req.assemble_prompt()
            resp = await client.chat(req)
            return (resp.text_response or "").strip() if resp else ""
        except Exception as e:
            logger.warning(f"LLM chat error in extractor: {e}")
            return ""

    # ==========================================
    # Fact extraction
    # ==========================================

    async def extract_personal_facts(self, conversation_text: str) -> list[dict]:
        """Extract per-user personal facts from a conversation segment."""
        if not self._llm_client:
            return []

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

        text = await self._chat(prompt)
        if text:
            return self._parse_json_array(text)
        return []

    async def extract_group_facts(self, conversation_text: str) -> list[dict]:
        """Extract group-level facts (atmosphere, topics, social dynamics)."""
        if not self._llm_client:
            return []

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
- "content": 事实描述，写成关于群聊的完整陈述句。涉及具体成员时必须用昵称。
- "importance": 重要性评分(1-10)
- "tags": 相关标签数组
- "semantic_id": 简短 snake_case 标识符（如 "group_discusses_ai_art"）

**严禁使用"该用户"等模糊代词。不要提取个人偏好/身份等个人事实。**

只输出 JSON 数组，不要有其他内容。如果没有值得记录的群组事实，输出空数组 []。"""

        text = await self._chat(prompt)
        if text:
            return self._parse_json_array(text)
        return []

    async def extract_facts(self, conversation_text: str) -> list[dict]:
        """Single-user extraction (PM-style conversations)."""
        if not self._llm_client:
            return []

        prompt = f"""分析以下对话片段，提取关键事实。忽略寒暄和无意义内容。
对话中用户的格式为 "昵称(ID): 内容"。

对话:
{conversation_text}

请以 JSON 数组格式输出，每条事实包含：
- "speaker_id": 该事实所属用户的 ID（从对话中括号内提取，如 "12345"）
- "subject": 该用户的昵称
- "content": 事实描述，用昵称作主语，写成完整陈述句。
- "importance": 重要性评分(1-10)
- "tags": 相关标签数组
- "semantic_id": 简短 snake_case 标识符（如 "xiaoming_likes_spicy"）

**严禁使用"该用户"等模糊代词，必须用具体昵称。**

只输出 JSON 数组，不要有其他内容。如果没有值得记录的事实，输出空数组 []。"""

        text = await self._chat(prompt)
        if text:
            return self._parse_json_array(text)
        return []

    # ==========================================
    # Self-awareness extraction (Phase 1: write-only)
    # ==========================================

    async def extract_self_awareness(
        self, conversation_text: str, ai_response_text: str = ""
    ) -> list[str]:
        """Extract observations about the AI's own behaviour in this turn."""
        if not self._llm_client:
            return []

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
- 每条觉察必须以"我"开头
- 只输出有价值的觉察
- 如果没有值得记录的行为觉察，直接输出 NONE
- 如果有，每条一行，最多2条

直接输出觉察内容或 NONE，不要有其他内容。"""

        text = await self._chat(prompt)
        if not text or text.upper() == "NONE":
            return []
        insights = [
            line.strip()
            for line in text.split("\n")
            if line.strip() and line.strip().upper() != "NONE"
        ]
        insights = [s for s in insights if s.startswith("我") and 5 < len(s) < 200]
        return insights[:2]

    # ==========================================
    # Semantic ID generation
    # ==========================================

    async def generate_semantic_id(self, content: str) -> str:
        """Ask the LLM to produce a snake_case slug; fall back to "" on failure."""
        if not self._llm_client:
            return ""

        prompt = f"""为以下记忆内容生成一个简短的 snake_case 文件名标识符（英文，无空格，不超过 30 字符）。
例如：hates_css, loves_python, pet_cat_xiaoju, prefers_dark_mode

内容: {content}

只输出标识符，不要有其他内容。"""

        slug = await self._chat(prompt, fast=True)
        if not slug:
            return ""
        slug = slug.strip().lower()
        slug = re.sub(r"[^a-z0-9_]", "_", slug)
        slug = re.sub(r"_+", "_", slug).strip("_")
        if slug and len(slug) <= 40:
            return slug
        return ""

    # ==========================================
    # Dedup
    # ==========================================

    async def deduplicate(
        self,
        new_content: str,
        entity_id: str,
        entity_type: str = "user",
        folder: str = "facts",
    ) -> tuple[str, Optional[Memory]]:
        """Two-level dedup: SHA-256 exact → FTS5 semantic + LLM."""
        content_hash = MemoryIndex.content_hash(new_content)
        exact_match = self.index.find_by_hash(
            content_hash, entity_id, entity_type, folder
        )
        if exact_match:
            logger.debug(f"Exact hash match: {new_content[:50]}...")
            return "duplicate", None

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

        for candidate in existing:
            decision = await self._check_conflict(new_content, candidate.text)
            if decision in ("duplicate", "update"):
                return decision, candidate

        return "new", None

    async def _check_conflict(self, new_content: str, existing_content: str) -> str:
        if not self._fast_or_default:
            return "new"

        prompt = f"""比较以下两条信息，判断它们的关系：

已有信息: {existing_content}
新信息: {new_content}

只输出以下三个选项之一：
- "duplicate"：新信息与已有信息基本相同，无需记录
- "update"：新信息是对已有信息的更新或补充，需要合并
- "new"：新信息与已有信息无关，是全新信息

只输出选项文本，不要有其他内容。"""

        text = await self._chat(prompt, fast=True)
        if text:
            result = text.strip().strip('"').lower()
            if result in ("duplicate", "update", "new"):
                return result
        return "new"

    # ==========================================
    # Merge
    # ==========================================

    async def merge_facts(self, existing_text: str, new_text: str) -> str:
        if not self._fast_or_default:
            return f"{existing_text}；{new_text}"

        prompt = f"""将以下两条信息合并为一条，保留所有有用信息：

已有信息: {existing_text}
新信息: {new_text}

直接输出合并后的结果，不要有其他内容。"""

        text = await self._chat(prompt, fast=True)
        if text:
            return text
        return f"{existing_text}；{new_text}"

    # ==========================================
    # Dedup + store (full pipeline)
    # ==========================================

    async def deduplicate_and_store(
        self,
        fact: dict,
        entity_id: str,
        entity_type: str = "user",
    ):
        """Full pipeline: dedup → merge / new write."""
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
            merged_text = await self.merge_facts(matched.text, content)
            matched.text = merged_text
            matched.importance = max(importance, matched.importance)
            matched.meta["last_accessed"] = time.time()

            existing_tags = set(matched.tags)
            existing_tags.update(tags)
            matched.tags = list(existing_tags)

            if await self.tree_store.update_memory(matched):
                logger.info(f"Memory merged: id={matched.id}")
            else:
                logger.warning(f"Failed to merge memory {matched.id}")
            return

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
    # Elevation (facts → reflections)
    # ==========================================

    async def check_elevation_trigger(
        self,
        entity_id: str,
        entity_type: str = "user",
    ) -> bool:
        facts = await self.tree_store.get_all_memories(
            entity_id=entity_id, entity_type=entity_type, folder="facts"
        )
        return len(facts) >= self.reflection_threshold

    async def generate_reflections(
        self,
        entity_id: str,
        entity_type: str = "user",
    ) -> list[str]:
        """Distill facts into reflections; archive absorbed low-importance facts."""
        if not self._llm_client:
            return []

        facts = await self.tree_store.get_all_memories(
            entity_id=entity_id, entity_type=entity_type, folder="facts"
        )
        if len(facts) < self.reflection_threshold:
            return []

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
        text = await self._chat(prompt)
        if not text:
            return []

        insights = [
            line.strip()
            for line in text.split("\n")
            if line.strip()
        ]

        for insight in insights:
            try:
                existing = await self.tree_store.search(
                    query=insight,
                    entity_id=entity_id,
                    entity_type=entity_type,
                    folder="reflections",
                    k=1,
                    update_access=False,
                )
                if existing:
                    merged = await self.merge_facts(existing[0].text, insight)
                    existing[0].text = merged
                    existing[0].meta["last_accessed"] = time.time()
                    await self.tree_store.update_memory(existing[0])
                    logger.debug(f"Reflection merged with existing: {insight[:50]}...")
                    continue

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
            except Exception as e:
                logger.error(f"Reflection generation error: {e}")

        # Archive absorbed low-importance facts
        if generated:
            for fact in facts:
                if fact.importance <= 4:
                    await self.tree_store.archive_memory(
                        memory_id=fact.id,
                        entity_id=entity_id,
                        entity_type=entity_type,
                        folder="facts",
                    )
                    logger.debug(f"Absorbed fact archived: {fact.id}")

        return generated

    # ==========================================
    # JSON parsing helper
    # ==========================================

    @staticmethod
    def _parse_json_array(text: str) -> list[dict]:
        text = text.strip()

        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()

        start = text.find("[")
        end = text.rfind("]")
        if start != -1 and end != -1 and end > start:
            text = text[start: end + 1]

        for attempt in range(3):
            try:
                if attempt == 1:
                    text = re.sub(r",\s*([}\]])", r"\1", text)
                if attempt == 2:
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
    cleaned = []
    for f in facts:
        if not isinstance(f, dict) or "content" not in f:
            continue
        raw_imp = f.get("importance")
        if raw_imp is None or raw_imp == "":
            f["importance"] = 5
        else:
            try:
                f["importance"] = max(1, min(10, int(float(raw_imp))))
            except (ValueError, TypeError):
                f["importance"] = 5
        if not isinstance(f.get("tags"), list):
            f["tags"] = []
        sem_id = f.get("semantic_id", "")
        if sem_id:
            sem_id = re.sub(r"[^a-z0-9_]", "_", sem_id.lower())
            sem_id = re.sub(r"_+", "_", sem_id).strip("_")
            f["semantic_id"] = sem_id
        cleaned.append(f)
    return cleaned
