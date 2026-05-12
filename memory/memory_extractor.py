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

import ast
import json
import re
import time
from typing import Optional

from core.logging_manager import get_logger
from .toml_tree_store import TomlTreeStore, Memory
from .memory_index import MemoryIndex

logger = get_logger("memory_extractor", "green")


class MemoryExtractor:
    """海马体：事实提取 → 去重 → 合并 → 升维"""

    def __init__(self, tree_store: TomlTreeStore, llm_client=None):
        self.tree_store = tree_store
        self.index: MemoryIndex = tree_store.index
        self._llm_client = llm_client
        self._fast_llm_client = None  # 轻量模型，用于去重/合并等低复杂度任务

        # 升维阈值：facts 积累达到此数量时触发反思
        self.reflection_threshold = 5

    def set_llm_client(self, llm_client):
        self._llm_client = llm_client

    def set_fast_llm_client(self, fast_llm_client):
        """设置轻量 LLM 客户端，用于去重/合并（回退到 _llm_client）"""
        self._fast_llm_client = fast_llm_client

    @property
    def _fast_or_default(self):
        """获取快速 LLM 客户端，未设置则回退到主 LLM"""
        return self._fast_llm_client or self._llm_client

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

        try:
            resp = await self._llm_client.chat([{"role": "user", "content": prompt}])
            if resp and resp.text_response:
                return self._parse_json_array(resp.text_response)
        except Exception as e:
            logger.error(f"Personal fact extraction error: {e}")
        return []

    async def extract_group_facts(self, conversation_text: str) -> list[dict]:
        """从对话中提取群组事实（群级）

        专注于群聊整体的信息：氛围、话题、成员关系、群体特征。
        结果将路由到群组 entity 目录下。

        Returns:
            [{"content": "...", "importance": 7, "tags": [...],
              "subject": "group", "semantic_id": "..."}, ...]
        """
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
- "content": 事实描述，写成关于群聊的完整陈述句。涉及具体成员时必须用昵称，例如：✅ "群里最近在讨论AI绘画" ✅ "小明和阿花经常在群里互怼" ✅ "群友们普遍偏好深夜聊天" ❌ "该用户经常发言"（禁止使用"该用户"，且这不是群级信息）
- "importance": 重要性评分(1-10)
- "tags": 相关标签数组
- "semantic_id": 简短 snake_case 标识符（如 "group_discusses_ai_art"）

**严禁使用"该用户""该成员""此人"等模糊代词，涉及具体人时用昵称。**
**不要提取个人偏好/身份等个人事实，那些由另一个流程处理。**

只输出 JSON 数组，不要有其他内容。如果没有值得记录的群组事实，输出空数组 []。"""

        try:
            resp = await self._llm_client.chat([{"role": "user", "content": prompt}])
            if resp and resp.text_response:
                return self._parse_json_array(resp.text_response)
        except Exception as e:
            logger.error(f"Group fact extraction error: {e}")
        return []

    async def extract_facts(self, conversation_text: str) -> list[dict]:
        """从对话中提取事实（私聊兼容接口）

        私聊场景只有一个用户，不需要双路径，走单次提取即可。
        """
        if not self._llm_client:
            return []

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
            resp = await self._llm_client.chat([{"role": "user", "content": prompt}])
            if resp and resp.text_response:
                return self._parse_json_array(resp.text_response)
        except Exception as e:
            logger.error(f"Fact extraction error: {e}")
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
- 每条觉察必须以"我"开头（例如："我在回答技术问题时倾向于给出过于详细的解释"）
- 只输出有价值的觉察，不要为了输出而输出
- 如果这次对话没有值得记录的行为觉察，直接输出 NONE
- 如果有，每条一行，最多2条

直接输出觉察内容或 NONE，不要有其他内容。"""

        try:
            resp = await self._llm_client.chat([{"role": "user", "content": prompt}])
            if resp and resp.text_response:
                text = resp.text_response.strip()
                if text.upper() == "NONE" or not text:
                    return []
                insights = [
                    line.strip()
                    for line in text.split("\n")
                    if line.strip() and line.strip().upper() != "NONE"
                ]
                # 过滤：必须以"我"开头，且长度合理
                insights = [
                    s for s in insights
                    if s.startswith("我") and 5 < len(s) < 200
                ]
                return insights[:2]  # 最多 2 条
        except Exception as e:
            logger.error(f"Self-awareness extraction error: {e}")
        return []

    # ==========================================
    # 语义 ID 生成
    # ==========================================

    async def generate_semantic_id(self, content: str) -> str:
        """让 LLM 生成语义化 slug ID

        回退策略：文本前缀 + hash
        """
        if not self._llm_client:
            return ""

        prompt = f"""为以下记忆内容生成一个简短的 snake_case 文件名标识符（英文，无空格，不超过 30 字符）。
例如：hates_css, loves_python, pet_cat_xiaoju, prefers_dark_mode

内容: {content}

只输出标识符，不要有其他内容。"""

        try:
            resp = await self._llm_client.chat([{"role": "user", "content": prompt}])
            if resp and resp.text_response:
                slug = resp.text_response.strip().lower()
                # 清理非法字符
                slug = re.sub(r"[^a-z0-9_]", "_", slug)
                slug = re.sub(r"_+", "_", slug).strip("_")
                if slug and len(slug) <= 40:
                    return slug
        except Exception as e:
            logger.debug(f"Semantic ID generation failed: {e}")
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
    ) -> tuple[str, Optional[Memory]]:
        """两级去重：SHA-256 精确匹配 → FTS5 语义搜索 + LLM 判断

        Returns:
            (decision, matched_memory)
            decision: "duplicate" | "update" | "new"
            matched_memory: 匹配到的旧记忆（仅 duplicate/update 时非 None）
        """
        # === 第一级：SHA-256 精确去重（零 LLM 调用） ===
        content_hash = MemoryIndex.content_hash(new_content)
        exact_match = self.index.find_by_hash(
            content_hash, entity_id, entity_type, folder
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
        client = self._fast_or_default
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
            if hasattr(client, "chat_fast"):
                resp = await client.chat_fast([{"role": "user", "content": prompt}])
            else:
                resp = await client.chat([{"role": "user", "content": prompt}])
            if resp and resp.text_response:
                result = resp.text_response.strip().strip('"').lower()
                if result in ("duplicate", "update", "new"):
                    return result
        except Exception as e:
            logger.error(f"Conflict check error: {e}")
        return "new"

    # ==========================================
    # 合并
    # ==========================================

    async def merge_facts(self, existing_text: str, new_text: str) -> str:
        """LLM 合并两条事实为一条（使用快速模型）"""
        client = self._fast_or_default
        if not client:
            return f"{existing_text}；{new_text}"

        prompt = f"""将以下两条信息合并为一条，保留所有有用信息：

已有信息: {existing_text}
新信息: {new_text}

直接输出合并后的结果，不要有其他内容。"""

        try:
            if hasattr(client, "chat_fast"):
                resp = await client.chat_fast([{"role": "user", "content": prompt}])
            else:
                resp = await client.chat([{"role": "user", "content": prompt}])
            if resp and resp.text_response:
                return resp.text_response.strip()
        except Exception as e:
            logger.error(f"Merge facts error: {e}")
        return f"{existing_text}；{new_text}"

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

            # 合并 tags
            existing_tags = set(matched.tags)
            existing_tags.update(tags)
            matched.tags = list(existing_tags)

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
        try:
            resp = await self._llm_client.chat([{"role": "user", "content": prompt}])
            if not (resp and resp.text_response):
                return []

            insights = [
                line.strip()
                for line in resp.text_response.strip().split("\n")
                if line.strip()
            ]

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
                    merged = await self.merge_facts(existing[0].text, insight)
                    existing[0].text = merged
                    existing[0].meta["last_accessed"] = time.time()
                    await self.tree_store.update_memory(existing[0])
                    logger.debug(f"Reflection merged with existing: {insight[:50]}...")
                    continue

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

            # 归档被吸收的低重要性 facts
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

        except Exception as e:
            logger.error(f"Reflection generation error: {e}")

        return generated

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
        # 标准化 importance
        raw_imp = f.get("importance")
        if raw_imp is None or raw_imp == "":
            f["importance"] = 5
        else:
            try:
                f["importance"] = max(1, min(10, int(float(raw_imp))))
            except (ValueError, TypeError):
                f["importance"] = 5
        # 确保 tags 是 list
        if not isinstance(f.get("tags"), list):
            f["tags"] = []
        # 清理 semantic_id
        sem_id = f.get("semantic_id", "")
        if sem_id:
            sem_id = re.sub(r"[^a-z0-9_]", "_", sem_id.lower())
            sem_id = re.sub(r"_+", "_", sem_id).strip("_")
            f["semantic_id"] = sem_id
        cleaned.append(f)
    return cleaned
