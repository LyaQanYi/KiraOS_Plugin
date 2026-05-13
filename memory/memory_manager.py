"""
双脑架构记忆管理器（重写版）

快系统（Fast Loop）：对话时检索记忆、维护短期对话历史
慢系统（Slow Loop）：后台海马体 — 提取事实、去重合并、升维反思、更新画像

完全基于 TomlTreeStore + EntityProfileStore，遵循 Memory Agent Charter。
"""

import asyncio
import json
import os
import time
from typing import Dict, Optional, List
from threading import Lock
from asyncio import Lock as AsyncLock

from core.logging_manager import get_logger

from .session import Session
from .memory_index import MemoryIndex
from .toml_tree_store import TomlTreeStore, Memory
from .entity_profile import EntityProfileStore, EntityProfile
from .memory_extractor import MemoryExtractor
from .memory_router import MemoryRouter
from .memory_paths import (
    ensure_directory_structure,
    list_all_entities,
    get_chat_memory_path,
    ENTITY_USER,
    ENTITY_GROUP,
)

logger = get_logger("kiraos_memory_manager", "green")


class MemoryManager:
    """双脑架构记忆管理器

    快系统：对话检索 + 短期对话历史
    慢系统：海马体后台处理（提取 → 去重 → 合并 → 升维 → 画像更新 → 遗忘）

    KiraOS_Plugin 适配版：移除了对 KiraConfig 的硬依赖，改为接收 plain 参数。
    """

    def __init__(
        self,
        *,
        max_memory_length: int = 20,
        llm_client=None,
        hippocampus_threshold: int = 3,
    ):
        self.max_memory_length = int(max_memory_length)
        self.chat_memory_path = get_chat_memory_path()

        self.memory_lock = AsyncLock()

        # === 初始化目录结构 ===
        ensure_directory_structure()

        # === 短期记忆 ===
        self.chat_memory = self._load_memory(self.chat_memory_path)
        self._ensure_memory_format()

        # === SQLite 持久化索引 ===
        self.memory_index = MemoryIndex()

        # === 长期记忆（TOML Tree Store + SQLite Index） ===
        self.tree_store = TomlTreeStore(index=self.memory_index)

        # === 实体画像 ===
        self.profile_store = EntityProfileStore()

        # === 海马体 ===
        self._llm_client = llm_client
        self.extractor = MemoryExtractor(self.tree_store, llm_client)
        self.router = MemoryRouter()

        # === 后台任务管理 ===
        self._pending_conversations: Dict[str, list] = {}
        self._hippocampus_threshold = int(hippocampus_threshold)
        self._hippocampus_lock = Lock()
        self._background_tasks: set = set()
        self._background_tasks_lock = Lock()

        # === Phase 1: 自我觉察采集（由 lifecycle 注入 PersonaEvolutionEngine） ===
        self._persona_evolution = None

        logger.info("MemoryManager initialized (dual-brain, JSON-based architecture)")

    def set_persona_evolution(self, engine):
        """注入 PersonaEvolutionEngine 实例，启用自我觉察采集"""
        self._persona_evolution = engine
        logger.info("PersonaEvolution engine connected to MemoryManager")

    async def async_init(self):
        """异步初始化：从 TOML 文件重建 SQLite 索引，确保一致性

        在 lifecycle.py 中创建 MemoryManager 后调用。
        TOML 文件是真相源（用户可直接编辑），SQLite 是运行时索引。
        每次启动全量 rebuild，保证两者一致。
        """
        await self.tree_store.rebuild_index()
        logger.info("Memory index rebuilt from TOML files (startup sync)")

    def set_llm_client(self, llm_client):
        """延迟设置 LLM 客户端"""
        self._llm_client = llm_client
        self.extractor.set_llm_client(llm_client)
        # 同时将 llm_client 作为 fast_llm 的后端（通过 chat_fast 方法自动选模型）
        self.extractor.set_fast_llm_client(llm_client)

    # ==========================================
    # 短期记忆（对话历史 — 滑动窗口）
    # ==========================================

    @staticmethod
    def _load_memory(path: str) -> Dict[str, dict]:
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    content = f.read()
                    return json.loads(content) if content.strip() else {}
            except Exception as e:
                logger.error(f"Error loading memory from {path}: {e}")
                return {}
        else:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            return {}

    def _ensure_memory_format(self):
        for session in self.chat_memory:
            session_content = self.chat_memory[session]
            if isinstance(session_content, dict):
                continue
            if isinstance(session_content, list):
                self.chat_memory[session] = {
                    "title": "",
                    "description": "",
                    "memory": session_content,
                }
        self._sync_save_memory(self.chat_memory, self.chat_memory_path)

    def _sync_save_memory(self, memory: Dict[str, dict] = None, path: str = None):
        if not memory:
            memory = self.chat_memory
        if not path:
            path = self.chat_memory_path
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(memory, f, indent=4, ensure_ascii=False)
        except Exception as e:
            logger.error(f"Error saving memory to {path}: {e}")

    async def _save_memory(self, memory: Dict[str, dict] = None, path: str = None):
        if not memory:
            memory = self.chat_memory
        if not path:
            path = self.chat_memory_path
        await asyncio.to_thread(self._sync_save_memory, memory, path)

    def get_session_info(self, session: str):
        parts = session.split(":", maxsplit=2)
        if len(parts) != 3:
            raise ValueError("Invalid session ID")
        if session not in self.chat_memory:
            self.chat_memory[session] = {"title": "", "description": "", "memory": []}
        return Session(
            adapter_name=parts[0],
            session_type=parts[1],
            session_id=parts[2],
            session_title=self.chat_memory[session]["title"],
            session_description=self.chat_memory[session]["description"],
        )

    async def update_session_info(
        self, session: str, title: str = None, description: str = None
    ):
        async with self.memory_lock:
            if session not in self.chat_memory:
                self.chat_memory[session] = {"title": "", "description": "", "memory": []}
            if title:
                self.chat_memory[session]["title"] = title
            if description:
                self.chat_memory[session]["description"] = description
            await self._save_memory()

    def get_memory_count(self, session: str) -> int:
        if session not in self.chat_memory:
            return 0
        return len(self.chat_memory[session].get("memory", []))

    def fetch_memory(self, session: str):
        if session not in self.chat_memory:
            self.chat_memory[session] = {"title": "", "description": "", "memory": []}
            return []
        mem_list = self.chat_memory[session].get("memory", [])
        messages = []
        for chunk in mem_list:
            for message in chunk:
                messages.append(message)
        return messages

    def read_memory(self, session: str):
        if session not in self.chat_memory:
            self.chat_memory[session] = {"title": "", "description": "", "memory": []}
            return []
        return self.chat_memory[session].get("memory", [])

    async def write_memory(self, session: str, memory: list[list[dict]]):
        async with self.memory_lock:
            # First write for a brand-new session would otherwise KeyError —
            # match the shape used by update_memory/read_memory/etc.
            if session not in self.chat_memory:
                self.chat_memory[session] = {"title": "", "description": "", "memory": []}
            self.chat_memory[session]["memory"] = memory
            await self._save_memory()
        logger.info(f"Memory written for {session}")

    async def update_memory(self, session: str, new_chunk):
        async with self.memory_lock:
            if session not in self.chat_memory:
                self.chat_memory[session] = {"title": "", "description": "", "memory": []}
            self.chat_memory[session]["memory"].append(new_chunk)
            if len(self.chat_memory[session]["memory"]) > self.max_memory_length:
                self.chat_memory[session]["memory"] = self.chat_memory[session]["memory"][1:]
            await self._save_memory()
        logger.info(f"Memory updated for {session}")

        # 触发海马体
        self._buffer_for_hippocampus(session, new_chunk)

    async def delete_session(self, session: str):
        async with self.memory_lock:
            self.chat_memory.pop(session, None)
            await self._save_memory()
        logger.info(f"Memory deleted for {session}")

    # ==========================================
    # 长期记忆检索（快系统）
    # ==========================================

    async def recall(
        self,
        query: str,
        entity_id: str = "",
        entity_type: str = "user",
        k: int = 5,
    ) -> List[Memory]:
        """检索相关长期记忆（对话前调用）

        跨 facts + reflections 联合检索，按综合分数排序。
        遵循宪章 §7：不在检索时 +1 access_count。
        """
        try:
            k = max(1, int(k))
        except (TypeError, ValueError):
            k = 5

        if not entity_id:
            return []

        try:
            return await self.tree_store.search_across_folders(
                query=query,
                entity_id=entity_id,
                entity_type=entity_type,
                folders=["facts", "reflections"],
                k=k,
            )
        except Exception as e:
            logger.error(f"Recall error: {e}")
            return []

    def format_recalled_memories(self, memories: List[Memory]) -> str:
        """格式化检索到的记忆为 Prompt 文本"""
        if not memories:
            return "暂无相关长期记忆"

        parts = []
        type_labels = {
            "fact": "事实",
            "reflection": "洞察",
            "episodic": "事件",
            "skill": "技能",
            "summary": "摘要",
        }
        for mem in memories:
            label = type_labels.get(mem.type, mem.type)
            tags_str = f" [{', '.join(mem.tags)}]" if mem.tags else ""
            parts.append(f"[{label}]{tags_str} {mem.raw_text}")
        return "\n".join(parts)

    async def confirm_memory_usage(self, memory_ids: list[str]):
        """确认记忆被实质使用，执行严格的 access_count +1

        宪章 §7.4：只有在记忆被实质引用时才 +1。
        由消息处理器在生成回复后调用。

        实现：依赖 `MemoryIndex.touch_access`，按 memory_id 直接定位行
        （索引以 id 为主键，无需额外的 entity 信息）。`touch_access` 是
        同步 SQLite 操作，整体 dispatch 到线程池避免阻塞 event loop。
        """
        if not memory_ids:
            return

        def _bulk_touch(ids: list[str]):
            for mid in ids:
                if not mid:
                    continue
                try:
                    self.memory_index.touch_access(mid)
                except Exception as e:
                    logger.warning(f"touch_access failed for {mid}: {e}")

        await asyncio.to_thread(_bulk_touch, list(memory_ids))

    # ==========================================
    # 实体画像
    # ==========================================

    async def get_profile(
        self, entity_id: str, entity_type: str = "user"
    ) -> EntityProfile:
        return await self.profile_store.get_profile(entity_id, entity_type)

    async def get_profile_prompt(
        self, entity_id: str, entity_type: str = "user"
    ) -> str:
        return await self.profile_store.get_profile_prompt(entity_id, entity_type)

    async def update_user_interaction(
        self, user_id: str, platform: str = "", nickname: str = ""
    ):
        """更新用户交互信息，同时维护 name/nickname 索引"""
        updates = {}
        if platform:
            updates["platform"] = platform
        if nickname:
            updates["nickname"] = nickname
        # "name 为空时回填 nickname" 的判断必须在 increment_interaction 的
        # critical section 内做，否则两次 IO 之间会被并发协程把正式 name 写
        # 进去后又覆盖回 nickname。把开关通过 kwarg 传下去即可。
        await self.profile_store.increment_interaction(
            user_id,
            ENTITY_USER,
            fill_name_from_nickname=bool(nickname),
            **updates,
        )

    # ==========================================
    # 海马体（慢系统 — 后台处理）
    # ==========================================

    def _buffer_for_hippocampus(self, session: str, new_chunk):
        """将新对话缓冲到待处理队列"""
        chunks_to_process = None
        with self._hippocampus_lock:
            if session not in self._pending_conversations:
                self._pending_conversations[session] = []
            self._pending_conversations[session].append(new_chunk)

            if len(self._pending_conversations[session]) >= self._hippocampus_threshold:
                chunks_to_process = self._pending_conversations[session][:]
                self._pending_conversations[session] = []

        if chunks_to_process is not None:
            try:
                loop = asyncio.get_running_loop()
                task = loop.create_task(
                    self._hippocampus_process(session, chunks_to_process)
                )
                with self._background_tasks_lock:
                    self._background_tasks.add(task)
                task.add_done_callback(self._on_background_task_done)
            except RuntimeError:
                with self._hippocampus_lock:
                    if session in self._pending_conversations:
                        self._pending_conversations[session] = (
                            chunks_to_process + self._pending_conversations[session]
                        )
                    else:
                        self._pending_conversations[session] = chunks_to_process
                logger.debug("No running event loop, skipping hippocampus processing")

    def _on_background_task_done(self, task: asyncio.Task):
        with self._background_tasks_lock:
            self._background_tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            logger.error(
                "Background hippocampus task failed",
                exc_info=(type(exc), exc, exc.__traceback__),
            )

    @staticmethod
    def _extract_sender_map(chunks: list) -> dict[str, str]:
        """从 chunk 元数据中程序化提取 sender 映射

        Returns:
            {nickname_lower: sender_id, ...}  和  {sender_id: sender_id, ...}
            双向映射，方便用 subject 或 speaker_id 匹配
        """
        sender_map: dict[str, str] = {}
        for chunk in chunks:
            for msg in chunk:
                if msg.get("role") != "user":
                    continue
                sid = msg.get("sender_id", "")
                name = msg.get("sender_name", "")
                if sid:
                    sender_map[sid] = sid  # ID → ID（精确匹配）
                    if name:
                        sender_map[name.lower()] = sid  # 昵称 → ID
        return sender_map

    @staticmethod
    def _get_unique_senders(chunks: list) -> list[str]:
        """获取 chunks 中所有不重复的 sender_id"""
        seen = set()
        result = []
        for chunk in chunks:
            for msg in chunk:
                if msg.get("role") != "user":
                    continue
                sid = msg.get("sender_id", "")
                if sid and sid not in seen:
                    seen.add(sid)
                    result.append(sid)
        return result

    def _resolve_fact_entity(
        self,
        fact: dict,
        adapter: str,
        sender_map: dict[str, str],
        unique_senders: list[str],
        session_entity_id: str,
        session_entity_type: str,
    ) -> tuple[str, str]:
        """程序化路由：决定一条 fact 应该存到哪个 entity

        路由优先级：
        1. LLM 给出的 speaker_id 在 sender_map 中 → 存到该用户
        2. LLM 给出的 subject 匹配 sender_map 中的昵称 → 存到该用户
        3. subject 明确为 "group" → 存到群组 entity
        4. 只有一个 sender（最常见情况）→ 所有非 group 事实都归该用户
        5. 兜底 → 存到 session entity（私聊=用户，群聊=群组）
        """
        speaker_id = fact.get("speaker_id", "").strip()
        subject = (fact.get("subject", "") or "").strip()

        # 1. LLM 输出了 speaker_id，且在 chunk 元数据中能找到
        if speaker_id and speaker_id in sender_map:
            return f"{adapter}:{sender_map[speaker_id]}", ENTITY_USER

        # 2. LLM 输出的 subject 匹配 chunk 中某个用户昵称
        if subject and subject.lower() in sender_map:
            matched_id = sender_map[subject.lower()]
            return f"{adapter}:{matched_id}", ENTITY_USER

        # 3. 明确的群组级信息
        if subject.lower() == "group":
            return session_entity_id, ENTITY_GROUP

        # 4. 如果整个对话只有一个 sender，所有个人事实都归这个人
        if len(unique_senders) == 1:
            return f"{adapter}:{unique_senders[0]}", ENTITY_USER

        # 5. 兜底：多人对话且无法匹配，存到 session entity
        return session_entity_id, session_entity_type

    async def _hippocampus_process(self, session: str, chunks: list):
        """海马体后台处理：提取事实 → 路由到正确 entity → 去重存储 → 升维反思 → 更新画像

        群聊走双路径（个人 + 群组分别提取），私聊走单路径。
        """
        if not self._llm_client:
            logger.debug("LLM client not set, skipping hippocampus processing")
            return

        try:
            # 1. 程序化提取 sender 映射（不依赖 LLM）
            sender_map = self._extract_sender_map(chunks)
            unique_senders = self._get_unique_senders(chunks)
            logger.debug(f"Hippocampus sender_map: {sender_map}, unique_senders: {unique_senders}")

            # 2. 解析 session 级别的 entity
            session_entity_id, session_entity_type = self._parse_entity_from_session(session)
            adapter = session.split(":", maxsplit=1)[0]
            is_group = session_entity_type == ENTITY_GROUP

            # 3. 提取事实（LLM）— 群聊双路径，私聊单路径
            conversation_text = self._chunks_to_text(chunks)

            # 构建 sender profile 上下文，辅助 LLM 更准确地提取和路由事实
            profile_context = await self._build_sender_profiles_context(
                adapter, unique_senders
            )
            if profile_context:
                conversation_text = f"{profile_context}\n\n{conversation_text}"

            if is_group:
                # 双路径并行提取
                personal_facts, group_facts = await asyncio.gather(
                    self.extractor.extract_personal_facts(conversation_text),
                    self.extractor.extract_group_facts(conversation_text),
                )
                logger.info(
                    f"Hippocampus dual-path: {len(personal_facts)} personal, "
                    f"{len(group_facts)} group facts"
                )
            else:
                personal_facts = await self.extractor.extract_facts(conversation_text)
                group_facts = []

            if not personal_facts and not group_facts:
                return

            # 4. 路由 + 去重存储
            routed_entities = set()

            # 4a. 个人事实 → 程序化路由到用户 entity
            for fact in personal_facts:
                entity_id, entity_type = self._resolve_fact_entity(
                    fact, adapter, sender_map, unique_senders,
                    session_entity_id, session_entity_type,
                )
                logger.debug(
                    f"Personal fact routed: '{fact.get('content', '')[:40]}...' "
                    f"→ {entity_type}:{entity_id}"
                )
                await self.extractor.deduplicate_and_store(
                    fact, entity_id, entity_type
                )
                routed_entities.add((entity_id, entity_type))

            # 4b. 群组事实 → 直接存到群组 entity
            for fact in group_facts:
                logger.debug(
                    f"Group fact stored: '{fact.get('content', '')[:40]}...' "
                    f"→ {ENTITY_GROUP}:{session_entity_id}"
                )
                await self.extractor.deduplicate_and_store(
                    fact, session_entity_id, ENTITY_GROUP
                )
                routed_entities.add((session_entity_id, ENTITY_GROUP))

            # 5. 检查升维触发（铁律 #2）— 对所有涉及的 entity 检查
            for eid, etype in routed_entities:
                if await self.extractor.check_elevation_trigger(eid, etype):
                    await self.extractor.generate_reflections(eid, etype)

            # 6. 更新画像（铁律 #3 — 只对用户 entity 更新画像）
            for fact in personal_facts:
                entity_id, entity_type = self._resolve_fact_entity(
                    fact, adapter, sender_map, unique_senders,
                    session_entity_id, session_entity_type,
                )
                if entity_type == ENTITY_USER:
                    await self._update_profile_from_facts(entity_id, entity_type, [fact])

            total = len(personal_facts) + len(group_facts)
            logger.info(
                f"Hippocampus completed for session {session}: "
                f"{total} facts ({len(personal_facts)} personal + {len(group_facts)} group), "
                f"senders={unique_senders}"
            )

            # 7. Phase 1: 自我觉察采集（只存不读）
            await self._collect_self_awareness(conversation_text)

        except Exception as e:
            logger.error(f"Hippocampus processing error: {e}", exc_info=True)

    async def _collect_self_awareness(self, conversation_text: str):
        """Phase 1: 从对话中提取 AI 的自我觉察，写入 global/self/facts/

        只存不读：写入的数据不会被召回到 LLM 上下文。
        失败不影响主流程（静默捕获异常）。
        """
        if not self._persona_evolution:
            return

        try:
            insights = await self.extractor.extract_self_awareness(conversation_text)
            if not insights:
                return

            for insight in insights:
                await self._persona_evolution.record_self_awareness(
                    content=insight,
                    importance=3,
                    tags=["auto-extracted", "phase1"],
                )
                logger.info(f"[Phase1] Self-awareness recorded: {insight[:60]}...")
        except Exception as e:
            # 静默失败：不影响主流程
            logger.warning(f"[Phase1] Self-awareness extraction failed: {e}")

    async def _update_profile_from_facts(
        self, entity_id: str, entity_type: str, facts: list[dict]
    ):
        """从提取的事实更新实体画像

        宪章 §4.3 优先级分层：importance >= 7 的事实写入 profile
        """
        for fact in facts:
            content = fact.get("content", "")
            importance = fact.get("importance", 5)
            if importance >= 7 and content:
                await self.profile_store.add_fact(entity_id, content, entity_type)

    # ==========================================
    # 遗忘周期（由外部调度器定时调用）
    # ==========================================

    async def run_forgetting_cycle(self):
        """执行遗忘周期：清理低价值记忆

        遗留接口，实际遗忘逻辑在 memory_decay.py 中实现。
        这里作为入口调用。
        """
        from .memory_decay import MemoryDecayEngine

        engine = MemoryDecayEngine(self.tree_store)
        removed, downgraded = await engine.run_full_cycle()

        if removed > 0 or downgraded > 0:
            logger.info(
                f"Forgetting cycle: removed={removed}, downgraded={downgraded}"
            )

    # ==========================================
    # 工具方法
    # ==========================================

    async def _build_sender_profiles_context(
        self, adapter: str, unique_senders: set
    ) -> str:
        """为海马体提取构建 sender profile 摘要

        让 LLM 在提取事实时知道每个 sender 的已知信息（昵称、曾用名、特征等），
        避免重复提取已有事实，并辅助 entity 路由。
        """
        if not unique_senders:
            return ""

        parts = []
        for sid in unique_senders:
            entity_id = f"{adapter}:{sid}"
            try:
                profile = await self.profile_store.get_profile(entity_id, ENTITY_USER)
                # 用昵称/名字标识，避免暴露系统 entity_id
                label = profile.name or profile.nickname or str(sid)
                info = []
                if profile.name:
                    info.append(f"名字: {profile.name}")
                if profile.nickname and profile.nickname != profile.name:
                    info.append(f"当前昵称: {profile.nickname}")
                if profile.aliases:
                    info.append(f"曾用名: {', '.join(profile.aliases)}")
                if profile.traits:
                    info.append(f"特征: {', '.join(profile.traits)}")
                if profile.facts:
                    info.append(f"已知事实: {'; '.join(profile.facts[:5])}")
                if info:
                    parts.append(f"【{label}】 {' | '.join(info)}")
            except Exception:
                continue

        if not parts:
            return ""
        return (
            "## 参与者已知信息（以下是对话中提到的用户的已有画像，"
            "提取事实时请避免重复记录这些已有内容）\n"
            + "\n".join(parts)
        )

    @staticmethod
    def _chunks_to_text(chunks: list) -> str:
        lines = []
        for chunk in chunks:
            for msg in chunk:
                role = msg.get("role", "unknown")
                content = msg.get("content", "")
                if role == "user":
                    sender_name = msg.get("sender_name", "User")
                    sender_id = msg.get("sender_id", "")
                    label = f"{sender_name}({sender_id})" if sender_id else "User"
                    lines.append(f"{label}: {content}")
                elif role == "assistant":
                    lines.append(f"Bot: {content}")
        return "\n".join(lines)

    @staticmethod
    def _parse_entity_from_session(session: str) -> tuple[str, str]:
        """从 session ID 解析实体信息

        session 格式: adapter:type:id
        - 私聊: adapter:pm:user_id → (adapter:user_id, "user")
        - 群聊: adapter:gm:group_id → (adapter:group_id, "group")
        """
        parts = session.split(":", maxsplit=2)
        if len(parts) != 3:
            raise ValueError(f"Invalid session ID: {session}")

        adapter = parts[0]
        session_type = parts[1]
        session_id = parts[2]

        if session_type == "gm":
            return f"{adapter}:{session_id}", ENTITY_GROUP
        return f"{adapter}:{session_id}", ENTITY_USER
