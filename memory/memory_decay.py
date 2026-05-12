"""
遗忘与衰减引擎

实现宪章 §4.4 的动态遗忘机制:
- 自动衰减：importance 随时间和未访问自然降低
- 主动降级：过时事实被新事实取代时主动下调
- 垃圾回收：importance ≤ 3 且长期未访问 → archive 或删除

Meta 数据完全存储在 SQLite 索引中，不再读取 TOML 文件的 meta。
"""

import time
from typing import List, Dict, Any

from core.logging_manager import get_logger
from .toml_tree_store import TomlTreeStore, Memory
from .memory_index import MemoryIndex
from .memory_paths import list_all_entities

logger = get_logger("memory_decay", "green")


class MemoryDecayEngine:
    """记忆衰减与遗忘引擎"""

    # 保留度评分阈值
    THRESHOLD_DELETE = 0.2
    THRESHOLD_DOWNGRADE = 0.4

    # 垃圾回收阈值
    GC_IMPORTANCE_THRESHOLD = 3
    GC_UNACCESSED_DAYS = 30

    # 衰减参数
    DECAY_INTERVAL_DAYS = 14

    def __init__(self, tree_store: TomlTreeStore):
        self.tree_store = tree_store
        self.index: MemoryIndex = tree_store.index

    # ==========================================
    # 保留度评分
    # ==========================================

    @staticmethod
    def calculate_retention_score(
        meta: Dict[str, Any], now: float = None
    ) -> float:
        """计算记忆保留分数 (0.0 ~ 1.0)

        接受 dict 或 Memory 作为输入。
        """
        if now is None:
            now = time.time()

        # 兼容 Memory 对象
        if isinstance(meta, Memory):
            importance = meta.importance
            access_count = meta.access_count
            timestamp = meta.timestamp
            last_accessed = meta.last_accessed
            mem_type = meta.type
        else:
            importance = meta.get("importance", 5)
            access_count = meta.get("access_count", 0)
            timestamp = meta.get("timestamp", now)
            last_accessed = meta.get("last_accessed", timestamp)
            mem_type = meta.get("memory_type", "fact")

        days_since_creation = max(0, (now - timestamp) / 86400)
        days_since_access = max(0, (now - last_accessed) / 86400)

        importance_score = importance / 10.0
        access_decay = 0.5 ** (days_since_access / 30.0)
        creation_decay = 0.5 ** (days_since_creation / 90.0)
        access_bonus = min(access_count * 0.05, 0.3)
        type_bonus = 0.2 if mem_type == "reflection" else 0.0

        score = (
            importance_score * 0.35
            + access_decay * 0.25
            + creation_decay * 0.1
            + access_bonus
            + type_bonus
        )
        return min(1.0, score)

    # ==========================================
    # 垃圾回收（直接操作索引）
    # ==========================================

    async def garbage_collect(
        self,
        entity_id: str,
        entity_type: str,
        folder: str = "facts",
    ) -> tuple[int, int]:
        """对指定范围执行垃圾回收

        直接从 SQLite 索引读取 meta，避免逐文件扫描。
        """
        metas = self.index.list_memories(
            entity_id=entity_id, entity_type=entity_type, folder=folder
        )

        if not metas:
            return 0, 0

        now = time.time()
        deleted = 0
        downgraded = 0

        for meta in metas:
            mem_id = meta["id"]
            score = self.calculate_retention_score(meta, now)

            if score < self.THRESHOLD_DELETE:
                if await self.tree_store.archive_memory(
                    memory_id=mem_id,
                    entity_id=entity_id,
                    entity_type=entity_type,
                    folder=folder,
                ):
                    deleted += 1
                continue

            mem_type = meta.get("memory_type", "fact")
            if score < self.THRESHOLD_DOWNGRADE and mem_type == "fact":
                old_imp = meta.get("importance", 5)
                new_imp = max(1, old_imp - 1)
                if new_imp != old_imp:
                    self.index.update_meta(mem_id, importance=new_imp)
                    downgraded += 1
                continue

            # GC: importance ≤ 3 且长期未访问
            importance = meta.get("importance", 5)
            if importance <= self.GC_IMPORTANCE_THRESHOLD and mem_type == "fact":
                last_accessed = meta.get("last_accessed", 0)
                days_unaccessed = (now - last_accessed) / 86400
                if days_unaccessed >= self.GC_UNACCESSED_DAYS:
                    await self.tree_store.archive_memory(
                        memory_id=mem_id,
                        entity_id=entity_id,
                        entity_type=entity_type,
                        folder=folder,
                    )
                    deleted += 1

        return deleted, downgraded

    # ==========================================
    # 完整遗忘周期
    # ==========================================

    async def run_full_cycle(self) -> tuple[int, int]:
        """扫描所有实体执行完整遗忘周期"""
        total_deleted = 0
        total_downgraded = 0

        entities = list_all_entities()

        for entity_id, entity_type in entities:
            for folder in ("facts", "reflections"):
                try:
                    d, dg = await self.garbage_collect(entity_id, entity_type, folder)
                    total_deleted += d
                    total_downgraded += dg
                except Exception as e:
                    logger.error(
                        f"Forgetting cycle error for {entity_type}:{entity_id}/{folder}: {e}"
                    )

        # 全局域
        from .memory_paths import get_global_self_dir
        global_self = get_global_self_dir()

        for subfolder in ("facts",):
            try:
                metas = self.index.list_memories(
                    base_dir=global_self, folder=subfolder
                )
                now = time.time()
                for meta in metas:
                    score = self.calculate_retention_score(meta, now)
                    if score < self.THRESHOLD_DELETE:
                        await self.tree_store.delete_memory(
                            memory_id=meta["id"],
                            base_dir=global_self,
                            folder=subfolder,
                        )
                        total_deleted += 1
            except Exception as e:
                logger.error(f"Forgetting cycle error for global/self/{subfolder}: {e}")

        return total_deleted, total_downgraded
