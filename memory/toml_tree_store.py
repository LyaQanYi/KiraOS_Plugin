"""
TOML Tree Store — 基于文件树 + SQLite 索引的记忆存储引擎

架构分离:
- TOML 文件：人类可读的内容文件 → 真相源（用户可直接编辑）
- SQLite（MemoryIndex）：运行时 meta（access_count、last_accessed 等）→ 索引与查询

TOML 文件 Schema（扁平结构）:
    # 语义注释
    id = "hates_css"
    type = "fact"
    text = "用户讨厌写 CSS，觉得前端很烦"
    importance = 6
    tags = ["frontend", "preference"]

    [source]
    session = "telegram:pm:12345"
    time = 2026-03-01T14:30:00+08:00

兼容性:
- Python 3.11+ → 内置 tomllib
- Python 3.10  → tomli 回退
- 写入统一使用 tomli_w
"""

import os
import time
import asyncio
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field

try:
    import tomllib  # Python 3.11+
except ImportError:
    import tomli as tomllib  # Python 3.10 fallback

import tomli_w

from core.logging_manager import get_logger
from .memory_paths import (
    get_entities_dir,
    get_global_dir,
    get_memory_root,
    get_entity_folder,
    ensure_entity_dirs,
)
from .memory_index import MemoryIndex

logger = get_logger("kiraos_toml_tree_store", "green")


# ========== 数据模型 ==========


@dataclass
class Memory:
    """一条记忆实体

    TOML 文件存储: id, type, text, importance, tags, [source]
    运行时 meta（access_count、last_accessed 等）由 SQLite 管理。
    """

    id: str  # 语义化 slug，如 "hates_css"
    type: str  # fact | reflection
    text: str = ""
    importance: int = 5
    tags: list = field(default_factory=list)
    source: dict = field(default_factory=dict)

    # 运行时 meta（来自 SQLite，不写入 TOML 文件）
    meta: dict = field(default_factory=dict)

    # 存储定位信息（不序列化）
    _entity_id: str = field(default="", repr=False)
    _entity_type: str = field(default="", repr=False)
    _folder: str = field(default="", repr=False)
    _base_dir: str = field(default="", repr=False)

    # === 便捷属性（兼容旧接口） ===

    @property
    def raw_text(self) -> str:
        return self.text

    @property
    def access_count(self) -> int:
        return self.meta.get("access_count", 0)

    @property
    def last_accessed(self) -> float:
        return self.meta.get("last_accessed", self.meta.get("timestamp", 0))

    @property
    def timestamp(self) -> float:
        return self.meta.get("timestamp", 0)

    @property
    def file_path(self) -> str:
        if self._base_dir:
            # `_base_dir` 是逻辑命名空间（"global"、"global/self"），不是真实
            # 磁盘路径。必须挂到 `get_memory_root()` 才能落到配置的数据根
            # 目录；直接拼会让全局记忆落到进程 cwd 下，跨命名空间整条串掉。
            d = os.path.join(get_memory_root(), self._base_dir)
            if self._folder:
                d = os.path.join(d, self._folder)
            return os.path.join(d, f"{self.id}.toml")
        else:
            d = get_entity_folder(self._entity_id, self._entity_type, self._folder)
            return os.path.join(d, f"{self.id}.toml")

    # === 序列化 ===

    def to_toml_dict(self) -> dict:
        """序列化为 TOML 文件格式（人类可读，无运行时 meta）"""
        d = {
            "id": self.id,
            "type": self.type,
            "text": self.text,
            "importance": self.importance,
            "tags": self.tags,
        }
        if self.source:
            d["source"] = self.source
        return d

    def to_full_dict(self) -> dict:
        """序列化为完整格式（含运行时 meta，用于归档/API）"""
        d = self.to_toml_dict()
        d["meta"] = self.meta
        return d

    @classmethod
    def from_toml_dict(
        cls, data: dict, runtime_meta: dict = None, **location_kwargs
    ) -> "Memory":
        """从 TOML 文件数据 + SQLite 运行时 meta 反序列化。

        显式做字段容错——TOML 是「用户可手工编辑」的真相源，他们写错
        类型（importance 写成字符串、tags 写成 dict 等等）时整条 memory
        不应该读失败或被跳过；类型不符就回退到安全默认值，让记忆仍可
        被加载。
        """
        # importance: int 或可转 int 的字符串，否则 5；夹到 [1,10]
        raw_importance = data.get("importance", 5)
        try:
            importance = max(1, min(10, int(raw_importance)))
        except (TypeError, ValueError):
            importance = 5

        # tags 必须是 list；不是就丢回空列表
        tags = data.get("tags", [])
        if not isinstance(tags, list):
            tags = []

        # source 必须是 dict；不是就丢回空字典
        source = data.get("source", {})
        if not isinstance(source, dict):
            source = {}

        return cls(
            id=data.get("id", ""),
            type=data.get("type", "fact"),
            text=data.get("text", ""),
            importance=importance,
            tags=tags,
            source=source,
            meta=runtime_meta or {},
            **location_kwargs,
        )

    @classmethod
    def from_legacy_json(cls, data: dict, **location_kwargs) -> "Memory":
        """兼容旧 JSON 格式（迁移用）"""
        meta = data.get("meta", {})
        content = data.get("content", {})
        return cls(
            id=data.get("id", ""),
            type=data.get("type", "fact"),
            text=content.get("raw_text", ""),
            importance=meta.get("importance", 5),
            tags=meta.get("tags", []),
            source=meta.get("source", {}),
            meta=meta,
            **location_kwargs,
        )

    def touch_access(self):
        """标记一次访问"""
        self.meta["access_count"] = self.access_count + 1
        self.meta["last_accessed"] = time.time()


# ========== 存储引擎 ==========


class TomlTreeStore:
    """基于 TOML 文件 + SQLite 索引的记忆管理系统

    TOML 文件: 人类可读的内容（id, type, text, importance, tags, [source]）
    SQLite: 运行时 meta + FTS5 全文索引 + 可选向量索引
    """

    def __init__(self, index: MemoryIndex = None):
        os.makedirs(get_entities_dir(), exist_ok=True)
        os.makedirs(get_global_dir(), exist_ok=True)

        self.index = index or MemoryIndex()

        # 读写锁 per (entity_type, entity_id, folder, base_dir)。无上限增长在
        # 长跑会变成内存泄漏（每个出现过的 entity/folder 都留一把锁），所以
        # 加 `_LOCK_CAP` 上限 + 懒回收 unlocked 锁——同 key 同锁的语义只在
        # 调用方持有该锁期间保证，释放后即可被驱逐，下次再 `_get_lock` 同 key
        # 会拿到新锁但因为没有 in-flight 操作所以行为等价。
        self._locks: Dict[str, asyncio.Lock] = {}
        logger.info("TomlTreeStore initialized (TOML files + SQLite index)")

    _LOCK_CAP = 256

    def _get_lock(self, key: str) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is not None:
            return lock
        # 创建新锁前，超 cap 时先扫一遍把所有未被持有的旧锁清掉。`asyncio.Lock`
        # 是单 event loop 内的协作锁；这个方法本身是 sync 的，asyncio 协作式
        # 调度保证遍历期间不会有别的协程修改 `_locks`。
        if len(self._locks) >= self._LOCK_CAP:
            stale = [k for k, lk in self._locks.items() if not lk.locked()]
            for k in stale:
                del self._locks[k]
                if len(self._locks) < self._LOCK_CAP:
                    break
        lock = asyncio.Lock()
        self._locks[key] = lock
        return lock

    @staticmethod
    def _resolve_dir(
        entity_id: str = "",
        entity_type: str = "",
        folder: str = "facts",
        base_dir: str = "",
    ) -> str:
        if base_dir:
            # 同 `Memory.file_path` 的逻辑——`base_dir` 是 data_root 下的逻辑
            # 命名空间，不是 cwd 相对路径，必须先挂到 data_root 上再拼 folder。
            d = os.path.join(get_memory_root(), base_dir)
            if folder:
                d = os.path.join(d, folder)
            return d
        else:
            return get_entity_folder(entity_id, entity_type, folder)

    @staticmethod
    def _cache_key(
        entity_id: str = "", entity_type: str = "", folder: str = "", base_dir: str = ""
    ) -> str:
        if base_dir:
            return f"global:{base_dir}:{folder}"
        return f"{entity_type}:{entity_id}:{folder}"

    # ==========================================
    # CRUD 操作
    # ==========================================

    async def add_memory(
        self,
        content_text: str,
        memory_type: str = "fact",
        importance: int = 5,
        tags: list = None,
        source: dict = None,
        semantic_id: str = "",
        entity_id: str = "",
        entity_type: str = "user",
        folder: str = "facts",
        base_dir: str = "",
    ) -> Memory:
        """写入一条新的记忆

        Args:
            semantic_id: 语义化 ID（如 "hates_css"），空则自动生成
        """
        now = time.time()
        mem_id = semantic_id if semantic_id else self._generate_fallback_id(content_text)

        source_data = source or {}
        if "time" not in source_data:
            source_data["time"] = datetime.now(timezone.utc).isoformat()

        memory = Memory(
            id=mem_id,
            type=memory_type,
            text=content_text,
            importance=max(1, min(10, importance)),
            tags=tags or [],
            source=source_data,
            meta={
                "importance": max(1, min(10, importance)),
                "timestamp": now,
                "access_count": 0,
                "last_accessed": now,
                "tags": tags or [],
                "source": source_data,
            },
            _entity_id=entity_id,
            _entity_type=entity_type,
            _folder=folder,
            _base_dir=base_dir,
        )

        # 确保目录存在
        if not base_dir and entity_id:
            ensure_entity_dirs(entity_id, entity_type)

        lock_key = self._cache_key(entity_id, entity_type, folder, base_dir)
        async with self._get_lock(lock_key):
            # 0. 处理 semantic_id 冲突 —— 不同事实生成同一 slug 时，给 id 加
            #    内容 hash 后缀避免覆盖旧 TOML。内容完全相同（hash 相等）则
            #    沿用旧 id 走 upsert 路径，保留访问历史。
            new_hash = MemoryIndex.content_hash(content_text)
            target_path = memory.file_path
            if os.path.exists(target_path):
                # `existing_hash is None` 用来区分"读失败"和"读成功但 hash 匹配"
                # —— 之前把读失败也置成空字符串后继续 fall-through 写入路径，结果
                # 一个暂时不可读（权限抖动 / 半文件 / 磁盘错误）的真相源文件
                # 会被新 memory 直接覆盖，原内容彻底蒸发。读失败时也强制走
                # 后缀路径，绝不覆盖未确认的现有文件。
                try:
                    existing = await asyncio.to_thread(self._sync_read_toml, target_path)
                    existing_hash = MemoryIndex.content_hash(existing.get("text", ""))
                except Exception as e:
                    logger.warning(
                        "Unreadable existing memory at %s, avoiding overwrite: %s",
                        target_path, e,
                    )
                    existing_hash = None
                # 三种情况都加后缀：
                # 1) 读成功且 hash 不同 → 冲突，正常加后缀
                # 2) 读失败（existing_hash is None）→ 保守加后缀避免覆盖
                # 只有 hash 完全相同时（同一条 memory 重写）才允许 upsert 同名
                if existing_hash is None or existing_hash != new_hash:
                    suffix = new_hash[:8]
                    memory.id = f"{mem_id}_{suffix}"
                    mem_id = memory.id
                    logger.info(
                        f"semantic_id collision on {entity_type}:{entity_id}/{folder} — "
                        f"using suffixed id {mem_id}"
                    )

            # 1. 写 TOML 文件（人类可读内容）
            await asyncio.to_thread(self._sync_write_toml, memory)

            # 2. 写 SQLite 索引（运行时 meta + 全文）
            await asyncio.to_thread(
                self.index.upsert,
                memory_id=mem_id,
                raw_text=content_text,
                memory_type=memory_type,
                importance=memory.importance,
                tags=memory.tags,
                source=source_data,
                entity_id=entity_id,
                entity_type=entity_type,
                folder=folder,
                base_dir=base_dir,
                file_path=memory.file_path,
                timestamp=now,
                last_accessed=now,
                access_count=0,
            )

        logger.debug(
            f"Memory added: type={memory_type}, id={mem_id}, "
            f"entity={entity_type}:{entity_id}, folder={folder}"
        )
        return memory

    async def update_memory(self, memory: Memory) -> bool:
        """更新记忆（TOML 文件内容 + 索引 meta）"""
        lock_key = self._cache_key(
            memory._entity_id, memory._entity_type, memory._folder, memory._base_dir
        )
        async with self._get_lock(lock_key):
            try:
                # 1. 更新 TOML 文件
                await asyncio.to_thread(self._sync_write_toml, memory)

                # 2. 更新索引
                await asyncio.to_thread(
                    self.index.upsert,
                    memory_id=memory.id,
                    raw_text=memory.text,
                    memory_type=memory.type,
                    importance=memory.importance,
                    tags=memory.tags,
                    source=memory.source,
                    entity_id=memory._entity_id,
                    entity_type=memory._entity_type,
                    folder=memory._folder,
                    base_dir=memory._base_dir,
                    file_path=memory.file_path,
                    timestamp=memory.timestamp,
                    last_accessed=memory.last_accessed,
                    access_count=memory.access_count,
                )
                return True
            except Exception as e:
                logger.error(f"Failed to update memory {memory.id}: {e}")
                return False

    async def get_memory(
        self,
        memory_id: str,
        entity_id: str = "",
        entity_type: str = "user",
        folder: str = "facts",
        base_dir: str = "",
    ) -> Optional[Memory]:
        """精确获取一条记忆（TOML 文件内容 + 索引 meta）"""
        d = self._resolve_dir(entity_id, entity_type, folder, base_dir)
        fpath = os.path.join(d, f"{memory_id}.toml")

        if not os.path.exists(fpath):
            return None

        try:
            # 读 TOML 文件内容
            file_data = await asyncio.to_thread(self._sync_read_toml, fpath)

            # 文件名（或调用方传的 `memory_id`）才是 canonical storage key。
            # 用户手改 TOML 把 `id` 改成别的值后，如果原样回填到 Memory.id，
            # WebUI / update_memory 会按新 id 写出第二个文件，原文件 +
            # 索引行变成孤儿，后续 get/archive 还会 404。只警告不信任。
            file_id = file_data.get("id")
            if file_id and file_id != memory_id:
                logger.warning(
                    "TOML id mismatch at %s: file says %r, path key is %r — "
                    "using the path key (canonical)",
                    fpath, file_id, memory_id,
                )
            file_data["id"] = memory_id

            # 读索引 meta（按命名空间复合主键）
            idx_meta = await asyncio.to_thread(
                lambda: self.index.get_meta(
                    memory_id,
                    entity_id=entity_id,
                    entity_type=entity_type,
                    folder=folder,
                    base_dir=base_dir,
                )
            )
            runtime_meta = {}
            if idx_meta:
                runtime_meta = {
                    "importance": idx_meta.get("importance", 5),
                    "timestamp": idx_meta.get("timestamp", 0),
                    "access_count": idx_meta.get("access_count", 0),
                    "last_accessed": idx_meta.get("last_accessed", 0),
                    "tags": idx_meta.get("tags", []),
                    "source": idx_meta.get("source", {}),
                }

            return Memory.from_toml_dict(
                file_data,
                runtime_meta=runtime_meta,
                _entity_id=entity_id,
                _entity_type=entity_type,
                _folder=folder,
                _base_dir=base_dir,
            )
        except Exception as e:
            logger.error(f"Read memory error {memory_id}: {e}")
            return None

    async def delete_memory(
        self,
        memory_id: str,
        entity_id: str = "",
        entity_type: str = "user",
        folder: str = "facts",
        base_dir: str = "",
    ) -> bool:
        """物理删除一条记忆（文件 + 索引）"""
        d = self._resolve_dir(entity_id, entity_type, folder, base_dir)
        fpath = os.path.join(d, f"{memory_id}.toml")

        lock_key = self._cache_key(entity_id, entity_type, folder, base_dir)
        async with self._get_lock(lock_key):
            try:
                if os.path.exists(fpath):
                    await asyncio.to_thread(os.remove, fpath)
                # 按命名空间复合主键删除，避免跨实体撞 ID 时连带删错记忆
                await asyncio.to_thread(
                    lambda: self.index.delete(
                        memory_id,
                        entity_id=entity_id,
                        entity_type=entity_type,
                        folder=folder,
                        base_dir=base_dir,
                    )
                )
                logger.debug(f"Memory deleted: {memory_id}")
                return True
            except Exception as e:
                logger.error(f"Delete memory error {memory_id}: {e}")
        return False

    async def archive_memory(
        self,
        memory_id: str,
        entity_id: str = "",
        entity_type: str = "user",
        folder: str = "facts",
        base_dir: str = "",
    ) -> bool:
        """将记忆移入归档目录（TOML 格式，含完整 meta 方便恢复）

        全程持 per-(entity, folder) 锁，防止与 `update_memory`/`add_memory`
        竞态。`asyncio.Lock` 不可重入，所以这里直接调用裸操作，不再走
        `delete_memory`（它会再次尝试同一把锁导致死锁）。
        """
        from .memory_paths import get_archive_dir, _id_to_path_segment

        lock_key = self._cache_key(entity_id, entity_type, folder, base_dir)
        async with self._get_lock(lock_key):
            memory = await self.get_memory(
                memory_id, entity_id, entity_type, folder, base_dir
            )
            if not memory:
                return False

            # 归档目录是平铺的，memory_id 只在各自 entity/folder 内唯一。
            # 不同用户都生成 `likes_python` 时，后归档的会直接覆盖先归档的。
            # 这里把 entity_type/entity_id/folder 编进归档文件名做命名空间隔离；
            # entity_id 走 url-encode 防 Windows 非法字符。
            archive_dir = get_archive_dir()
            os.makedirs(archive_dir, exist_ok=True)
            if base_dir:
                ns = base_dir.replace("/", "_").replace("\\", "_")
                archive_name = f"{ns}__{folder}__{memory_id}.toml"
            else:
                archive_name = (
                    f"{entity_type}__{_id_to_path_segment(entity_id)}"
                    f"__{folder}__{memory_id}.toml"
                )
            archive_path = os.path.join(archive_dir, archive_name)

            try:
                # 1. 归档时写入完整数据（含 meta，方便恢复）
                full_data = memory.to_full_dict()
                await asyncio.to_thread(
                    self._sync_write_toml_to_path, full_data, archive_path
                )
                # 2. 删除源文件 + 索引（裸操作，避免锁重入）
                d = self._resolve_dir(entity_id, entity_type, folder, base_dir)
                fpath = os.path.join(d, f"{memory_id}.toml")
                if os.path.exists(fpath):
                    await asyncio.to_thread(os.remove, fpath)
                # 按命名空间复合主键删除索引行
                await asyncio.to_thread(
                    lambda: self.index.delete(
                        memory_id,
                        entity_id=entity_id,
                        entity_type=entity_type,
                        folder=folder,
                        base_dir=base_dir,
                    )
                )
                logger.debug(f"Memory archived: {memory_id}")
                return True
            except Exception as e:
                logger.error(f"Archive memory error {memory_id}: {e}")
                return False

    async def get_all_memories(
        self,
        entity_id: str = "",
        entity_type: str = "user",
        folder: str = "facts",
        base_dir: str = "",
    ) -> List[Memory]:
        """获取指定目录下所有记忆"""
        d = self._resolve_dir(entity_id, entity_type, folder, base_dir)
        if not os.path.exists(d):
            return []

        def _scan():
            mems = []
            for fname in os.listdir(d):
                if not fname.endswith(".toml"):
                    continue
                fpath = os.path.join(d, fname)
                try:
                    data = self._sync_read_toml(fpath)
                    # 文件名（去掉 .toml）才是 canonical storage key——
                    # 不能信任 TOML 文件内部的 `id` 字段（参考 get_memory
                    # 的同款处理）。用户手改后再回填会让上层用错 id 去更新
                    # / 归档，产生孤儿文件 + 索引残骸。
                    mem_id = fname[:-5]
                    file_id = data.get("id")
                    if file_id and file_id != mem_id:
                        logger.warning(
                            "TOML id mismatch at %s: file says %r, path key is %r"
                            " — using the path key (canonical)",
                            fpath, file_id, mem_id,
                        )
                    data["id"] = mem_id

                    # 从索引读 runtime meta（按命名空间复合主键定位）
                    idx_meta = self.index.get_meta(
                        mem_id,
                        entity_id=entity_id,
                        entity_type=entity_type,
                        folder=folder,
                        base_dir=base_dir,
                    )
                    runtime_meta = {}
                    if idx_meta:
                        runtime_meta = {
                            "importance": idx_meta.get("importance", 5),
                            "timestamp": idx_meta.get("timestamp", 0),
                            "access_count": idx_meta.get("access_count", 0),
                            "last_accessed": idx_meta.get("last_accessed", 0),
                            "tags": idx_meta.get("tags", []),
                            "source": idx_meta.get("source", {}),
                        }

                    mems.append(
                        Memory.from_toml_dict(
                            data,
                            runtime_meta=runtime_meta,
                            _entity_id=entity_id,
                            _entity_type=entity_type,
                            _folder=folder,
                            _base_dir=base_dir,
                        )
                    )
                except Exception as e:
                    logger.warning(f"Could not load {fpath}: {e}")
            return mems

        return await asyncio.to_thread(_scan)

    # ==========================================
    # 检索（委托给 MemoryIndex）
    # ==========================================

    async def search(
        self,
        query: str,
        entity_id: str = "",
        entity_type: str = "user",
        folder: str = "facts",
        base_dir: str = "",
        k: int = 5,
        update_access: bool = False,
        query_embedding: list = None,
    ) -> List[Memory]:
        """混合检索（FTS5 + 可选向量）"""
        results = await asyncio.to_thread(
            self.index.hybrid_search,
            query=query,
            query_embedding=query_embedding,
            entity_id=entity_id,
            entity_type=entity_type,
            folder=folder,
            base_dir=base_dir,
            k=k,
        )

        if not results:
            return []

        memories = []
        for r in results:
            mem = Memory(
                id=r["id"],
                type=r.get("memory_type", "fact"),
                text=r.get("raw_text", ""),
                importance=r.get("importance", 5),
                tags=r.get("tags", []),
                source=r.get("source", {}),
                meta={
                    "importance": r.get("importance", 5),
                    "timestamp": r.get("timestamp", 0),
                    "access_count": r.get("access_count", 0),
                    "last_accessed": r.get("last_accessed", 0),
                    "tags": r.get("tags", []),
                    "source": r.get("source", {}),
                    # 保留混合检索 score（FTS5 BM25 + 向量相似度 + importance
                    # + 时间衰减综合），让跨 entity 全局搜索能据此重排，而不
                    # 是只能在 Memory 顶层字段里用 importance/last_accessed。
                    "_score": r.get("_score", 0.0),
                    "_vec_score": r.get("_vec_score", 0.0),
                },
                _entity_id=entity_id or r.get("entity_id", ""),
                _entity_type=entity_type or r.get("entity_type", ""),
                _folder=folder or r.get("folder", ""),
                _base_dir=base_dir or r.get("base_dir", ""),
            )

            # 尝试从 TOML 文件加载完整内容（可能有用户手动编辑的注释等）。
            # 用 `asyncio.to_thread` 把每条命中的同步读盘 offload 到线程池，
            # 否则在 k>1 的检索路径里事件循环会被磁盘 IO 接连卡住，WebUI
            # 搜索 / 后台任务都会一起抖。
            fpath = r.get("file_path", "") or mem.file_path
            if fpath and os.path.exists(fpath):
                try:
                    file_data = await asyncio.to_thread(self._sync_read_toml, fpath)
                    mem.text = file_data.get("text", mem.text)
                    mem.tags = file_data.get("tags", mem.tags)
                    mem.importance = file_data.get("importance", mem.importance)
                except Exception as e:
                    logger.debug(
                        "search: failed to load file %s for %s: %s",
                        fpath,
                        mem.id,
                        e,
                    )

            memories.append(mem)

        if update_access:
            for mem in memories:
                mem.touch_access()
                await asyncio.to_thread(
                    lambda m=mem: self.index.touch_access(
                        m.id,
                        entity_id=m._entity_id,
                        entity_type=m._entity_type,
                        folder=m._folder,
                        base_dir=m._base_dir,
                    )
                )

        return memories

    async def search_across_folders(
        self,
        query: str,
        entity_id: str = "",
        entity_type: str = "user",
        folders: list = None,
        k: int = 5,
        query_embedding: list = None,
    ) -> List[Memory]:
        """跨多个目录检索，合并排序"""
        if folders is None:
            folders = ["facts", "reflections"]

        all_results = []
        for folder in folders:
            results = await self.search(
                query=query,
                entity_id=entity_id,
                entity_type=entity_type,
                folder=folder,
                k=k,
                query_embedding=query_embedding,
            )
            all_results.extend(results)

        import math
        now = time.time()
        # 先按 search() 留在 mem.meta["_score"] 的混合检索分排，再用
        # importance + 时间衰减作为 tie-breaker。原来只看 importance / recency
        # 会把弱命中的高 importance 条目压过真正相关的结果——上层
        # `MemoryManager.recall()` 整条链路的搜索质量都会被这条排序拖累。
        all_results.sort(
            key=lambda m: (
                float((m.meta or {}).get("_score", 0.0)),
                m.importance * 0.6
                + math.exp(-(now - m.last_accessed) / 86400 / 30.0) * 0.4,
            ),
            reverse=True,
        )
        return all_results[:k]

    # ==========================================
    # 索引管理
    # ==========================================

    async def rebuild_index(self):
        """从文件系统重建 SQLite 索引（灾难恢复）"""
        await asyncio.to_thread(self.index.rebuild_index_from_files, get_memory_root())
        logger.info("Index rebuilt from files")

    async def ensure_indexed(self, memory: Memory):
        """确保单条记忆在索引中（用于旧文件迁移）"""
        existing = await asyncio.to_thread(
            lambda: self.index.get_meta(
                memory.id,
                entity_id=memory._entity_id,
                entity_type=memory._entity_type,
                folder=memory._folder,
                base_dir=memory._base_dir,
            )
        )
        if not existing:
            await asyncio.to_thread(
                self.index.upsert,
                memory_id=memory.id,
                raw_text=memory.text,
                memory_type=memory.type,
                importance=memory.importance,
                tags=memory.tags,
                source=memory.source,
                entity_id=memory._entity_id,
                entity_type=memory._entity_type,
                folder=memory._folder,
                base_dir=memory._base_dir,
                file_path=memory.file_path,
                timestamp=memory.timestamp,
                last_accessed=memory.last_accessed,
                access_count=memory.access_count,
            )

    # ==========================================
    # TOML 读写内部方法
    # ==========================================

    @staticmethod
    def _atomic_write_bytes(fpath: str, payload: bytes) -> None:
        """跨平台原子写：dump 到同目录 .tmp，fsync，再 os.replace 重命名。

        没有这一步的话，TOML 写到一半被读取协程读到，就会拿到半文件 ——
        而读取路径并不持锁。`os.replace` 是 atomic rename（同文件系统），
        保证读者永远看到完整的旧文件或完整的新文件。
        """
        os.makedirs(os.path.dirname(fpath), exist_ok=True)
        tmp = f"{fpath}.tmp"
        try:
            with open(tmp, "wb") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, fpath)
        except Exception:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass
            raise

    @classmethod
    def _sync_write_toml(cls, memory: Memory):
        """写入 TOML 文件（人类可读内容，无运行时 meta）；原子替换。"""
        fpath = memory.file_path
        data = memory.to_toml_dict()
        cls._atomic_write_bytes(fpath, tomli_w.dumps(data).encode("utf-8"))

    @staticmethod
    def _sync_read_toml(fpath: str) -> dict:
        """读取 TOML 文件"""
        with open(fpath, "rb") as f:
            return tomllib.load(f)

    @classmethod
    def _sync_write_toml_to_path(cls, data: dict, fpath: str):
        """写入 TOML 到指定路径；原子替换。"""
        # 过滤掉 None 值（TOML 不支持）
        clean = _clean_for_toml(data)
        cls._atomic_write_bytes(fpath, tomli_w.dumps(clean).encode("utf-8"))

    @staticmethod
    def _generate_fallback_id(text: str) -> str:
        """当 LLM 未生成语义 ID 时的回退策略：从文本中提取关键词"""
        import hashlib
        # 取前 8 字符的 hash 作为回退
        h = hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]
        # 尝试从文本中提取简短关键词
        cleaned = text.strip()[:20].replace(" ", "_").replace("/", "_")
        # 只保留安全字符
        safe = "".join(c for c in cleaned if c.isalnum() or c in ("_", "-"))
        if safe:
            return f"{safe}_{h}"
        return f"mem_{h}"


def _clean_for_toml(data: dict) -> dict:
    """递归清理字典，移除 TOML 不支持的类型（None 等）"""
    clean = {}
    for k, v in data.items():
        if v is None:
            continue
        elif isinstance(v, dict):
            clean[k] = _clean_for_toml(v)
        elif isinstance(v, (list, tuple)):
            clean[k] = [_clean_for_toml(i) if isinstance(i, dict) else i for i in v if i is not None]
        else:
            clean[k] = v
    return clean
