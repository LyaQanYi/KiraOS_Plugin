"""
SQLite 持久化索引层 — MemoryIndex

所有 meta 数据（importance、timestamps、tags、access_count 等）统一存储在 SQLite 中。
JSON 文件退化为纯内容文件（只保留 id、type、content）。

功能:
- FTS5 全文检索（替代内存 BM25）
- 结构化 meta 查询（importance、时间范围、tags）
- SHA-256 内容指纹用于快速去重
- 可选向量嵌入（sqlite-vec）+ 混合检索 + 优雅降级

参考 OpenClaw 架构:
- SQLite 作为持久化索引，文件作为内容真相源
- 嵌入缓存 + SHA-256 去重
- 增量索引（通过 content_hash 判断是否需要重新嵌入）
"""

import hashlib
import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Any, Dict, List, Optional, Tuple

from core.logging_manager import get_logger
from .memory_paths import get_index_db_path, _path_segment_to_id

logger = get_logger("kiraos_memory_index", "green")

# jieba 是中文分词的最佳选择，但不应该作为插件加载的硬依赖。
# 没装的话，降级为按字符切分 + ASCII whitespace —— FTS5 仍可工作，
# 只是中文查准率会差一些。运行时会打一次明显的 warning 提醒。
try:
    import jieba  # type: ignore
    _JIEBA_AVAILABLE = True
except ImportError:
    jieba = None  # type: ignore
    _JIEBA_AVAILABLE = False
    logger.warning(
        "jieba 未安装，中文记忆检索会降级为字符级分词。"
        "建议执行: pip install jieba"
    )


def _fallback_tokenize(text: str) -> list[str]:
    """无 jieba 时的兜底分词：按 whitespace 切英文，按字符切中文。

    对 FTS5 检索来说这并不完美（中文会变成单字 token），但比把整段中文当一个
    token 强得多——至少同字开头的句子能命中。
    """
    if not text:
        return []
    tokens: list[str] = []
    buf = ""
    for ch in text:
        # ASCII 字母数字 → 累积进 buf 当成一个 word
        if ch.isascii() and (ch.isalnum() or ch == "_"):
            buf += ch
        else:
            if buf:
                tokens.append(buf)
                buf = ""
            if not ch.isspace():
                tokens.append(ch)
    if buf:
        tokens.append(buf)
    return tokens


class MemoryIndex:
    """SQLite 持久化记忆索引

    职责:
    - 存储所有记忆的 meta 数据
    - 提供 FTS5 全文检索
    - 可选向量嵌入混合检索（优雅降级到纯 FTS5）
    - SHA-256 内容去重
    """

    def __init__(self, db_path: str = ""):
        # db_path 为空时延迟解析，确保插件 set_data_root() 后才取值
        self.db_path = db_path or get_index_db_path()
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._conn: Optional[sqlite3.Connection] = None
        self._vec_available = False
        self._embedder = None  # 延迟初始化
        # Python sqlite3 + threadsafety=1 模式下，多线程共享同一个 Connection
        # 是 *不安全* 的（官方文档明确要求由调用方串行化）。本类的所有 SQL
        # 操作都会经 `asyncio.to_thread(self.index.*)` 跑到不同 worker
        # 线程，所以这里用一把可重入锁包住所有访问。RLock 而非 Lock 是因为
        # `_try_init_vec` 在 `_init_db` 持锁期间还会再调一次 conn.execute。
        self._conn_lock = threading.RLock()
        self._init_db()

    def _init_db(self):
        """初始化数据库 schema"""
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")

        # 兼容旧 schema：v3 之前 `memories.id` 是全局 PRIMARY KEY，会让两个
        # 不同 entity 用同一语义 ID（如 likes_python）撞主键。新 schema 用
        # (entity_type, entity_id, folder, base_dir, id) 复合主键做命名空间
        # 隔离。检测到旧表就 DROP 重建——内容索引会在下一次 rebuild_index_
        # from_files() 调用时从 TOML 真相源完全恢复。
        try:
            row = self._conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='memories'"
            ).fetchone()
        except sqlite3.OperationalError:
            row = None
        if row and row[0] and "primary key (entity_type" not in row[0].lower():
            logger.warning(
                "Detected legacy memories schema (id PRIMARY KEY) — dropping for "
                "rebuild with composite namespace key. Run rebuild_index_from_files() "
                "afterwards to repopulate from TOML."
            )
            with self._transaction() as cur:
                cur.execute("DROP TABLE IF EXISTS memories_fts")
                cur.execute("DROP TABLE IF EXISTS memories")

        with self._transaction() as cur:
            # 主表：记忆元数据。复合主键防止 likes_python 这类语义 ID 在不同
            # entity 下撞车——撞了后写入会覆盖前者的索引，跨实体搜索丢条目、
            # touch_access 串线、meta 互污染都是这条主键的直接后果。
            #
            # `storage_key` 是个 STORED 生成列：拼接 entity 命名空间 + id 形成
            # 全局唯一 token，给 FTS5 / vec0 这些不能用复合主键的虚拟表做单
            # 字段 JOIN 用。需要 SQLite 3.31+（Python 3.10+ 默认满足）。
            cur.execute("""
                CREATE TABLE IF NOT EXISTS memories (
                    id TEXT NOT NULL,
                    entity_id TEXT NOT NULL DEFAULT '',
                    entity_type TEXT NOT NULL DEFAULT '',
                    folder TEXT NOT NULL DEFAULT 'facts',
                    memory_type TEXT NOT NULL DEFAULT 'fact',
                    importance INTEGER NOT NULL DEFAULT 5,
                    timestamp REAL NOT NULL,
                    last_accessed REAL NOT NULL,
                    access_count INTEGER NOT NULL DEFAULT 0,
                    tags TEXT NOT NULL DEFAULT '[]',
                    source TEXT NOT NULL DEFAULT '{}',
                    content_hash TEXT NOT NULL DEFAULT '',
                    file_path TEXT NOT NULL DEFAULT '',
                    base_dir TEXT NOT NULL DEFAULT '',
                    raw_text TEXT NOT NULL DEFAULT '',
                    storage_key TEXT GENERATED ALWAYS AS (
                        entity_type || char(1) || entity_id || char(1) ||
                        folder || char(1) || base_dir || char(1) || id
                    ) STORED,
                    PRIMARY KEY (entity_type, entity_id, folder, base_dir, id)
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_mem_storage_key
                ON memories(storage_key)
            """)

            # FTS5 全文索引（独立表，手动同步）
            cur.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
                    memory_id UNINDEXED,
                    raw_text,
                    tags_text,
                    tokenize='unicode61'
                )
            """)

            # 结构化查询索引
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_mem_entity
                ON memories(entity_type, entity_id, folder)
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_mem_importance
                ON memories(importance DESC)
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_mem_hash
                ON memories(content_hash)
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_mem_accessed
                ON memories(last_accessed)
            """)

        # 检测 sqlite-vec 扩展
        self._try_init_vec()

        logger.info(
            f"MemoryIndex initialized: db={self.db_path}, "
            f"vec_available={self._vec_available}"
        )

    def _try_init_vec(self):
        """尝试加载 sqlite-vec 扩展（优雅降级）"""
        try:
            import sqlite_vec  # noqa: F401
            self._conn.enable_load_extension(True)
            sqlite_vec.load(self._conn)
            self._conn.enable_load_extension(False)

            # 创建向量表
            self._conn.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS memories_vec USING vec0(
                    id TEXT PRIMARY KEY,
                    embedding float[768]
                )
            """)
            self._conn.commit()
            self._vec_available = True
            logger.info("sqlite-vec extension loaded, vector search enabled")
        except (ImportError, Exception) as e:
            self._vec_available = False
            logger.debug(f"sqlite-vec not available, using FTS5 only: {e}")

    @contextmanager
    def _transaction(self):
        """写事务上下文管理器；持 `_conn_lock` 全程串行化。"""
        with self._conn_lock:
            cur = self._conn.cursor()
            try:
                yield cur
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
            finally:
                cur.close()

    @contextmanager
    def _read(self):
        """只读路径的上下文：持 `_conn_lock` 但不开事务。

        所有直接调 `self._conn.execute(...).fetchall()` / `.fetchone()` 的
        读路径都要换成：
            with self._read() as conn:
                rows = conn.execute(sql, params).fetchall()
        以确保 connection 同一时刻只被一个线程使用（Python sqlite3
        threadsafety=1 限制）。
        """
        with self._conn_lock:
            yield self._conn

    # ==========================================
    # 内容哈希
    # ==========================================

    @staticmethod
    def content_hash(text: str) -> str:
        """SHA-256 内容指纹"""
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def _storage_key(
        memory_id: str,
        entity_id: str = "",
        entity_type: str = "",
        folder: str = "facts",
        base_dir: str = "",
    ) -> str:
        """Compose the namespaced storage key.

        FTS5 表无法对 (entity, folder, id) 复合主键直接做 WHERE，所以
        `memories_fts.memory_id` 列改为存这个拼接后的复合 key——和 main
        表的 PK 一一对应，DELETE/INSERT/JOIN 都按它走。分隔符用 `\x01`
        是为了避免和真实 ID 里的 `:` / `/` 之类字符撞。
        """
        return (
            f"{entity_type}\x01{entity_id}\x01{folder}\x01{base_dir}\x01{memory_id}"
        )

    @staticmethod
    def _segment_for_fts(text: str) -> str:
        """用 jieba（可选）分词后用空格连接，使 FTS5 unicode61 能正确检索中文"""
        if not text:
            return ""
        if _JIEBA_AVAILABLE:
            tokens = [t for t in jieba.lcut(text) if t.strip()]
        else:
            tokens = [t for t in _fallback_tokenize(text) if t.strip()]
        return " ".join(tokens)

    # ==========================================
    # CRUD 操作
    # ==========================================

    def upsert(
        self,
        memory_id: str,
        raw_text: str,
        memory_type: str = "fact",
        importance: int = 5,
        tags: list = None,
        source: dict = None,
        entity_id: str = "",
        entity_type: str = "",
        folder: str = "facts",
        base_dir: str = "",
        file_path: str = "",
        timestamp: float = 0,
        last_accessed: float = 0,
        access_count: int = 0,
    ):
        """插入或更新记忆元数据"""
        now = time.time()
        if not timestamp:
            timestamp = now
        if not last_accessed:
            last_accessed = now

        tags_json = json.dumps(tags or [], ensure_ascii=False)
        source_json = json.dumps(source or {}, ensure_ascii=False)
        chash = self.content_hash(raw_text)

        # jieba 分词后存入 FTS（确保中文可检索）
        segmented_text = self._segment_for_fts(raw_text)
        tags_flat = " ".join(tags or [])

        with self._transaction() as cur:
            cur.execute("""
                INSERT INTO memories
                    (id, entity_id, entity_type, folder, memory_type,
                     importance, timestamp, last_accessed, access_count,
                     tags, source, content_hash, file_path, base_dir, raw_text)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(entity_type, entity_id, folder, base_dir, id) DO UPDATE SET
                    memory_type = excluded.memory_type,
                    timestamp = excluded.timestamp,
                    importance = excluded.importance,
                    last_accessed = excluded.last_accessed,
                    access_count = excluded.access_count,
                    tags = excluded.tags,
                    source = excluded.source,
                    content_hash = excluded.content_hash,
                    file_path = excluded.file_path,
                    raw_text = excluded.raw_text
            """, (
                memory_id, entity_id, entity_type, folder, memory_type,
                importance, timestamp, last_accessed, access_count,
                tags_json, source_json, chash, file_path, base_dir, raw_text,
            ))

            # 同步 FTS 索引（用分词后的文本）。FTS5 行的 memory_id 列存复合
            # storage_key 而非裸语义 ID，跨 entity 重名时彼此不会互删/互查。
            storage_key = self._storage_key(
                memory_id, entity_id, entity_type, folder, base_dir
            )
            cur.execute(
                "DELETE FROM memories_fts WHERE memory_id = ?", (storage_key,)
            )
            cur.execute(
                "INSERT INTO memories_fts(memory_id, raw_text, tags_text) VALUES (?, ?, ?)",
                (storage_key, segmented_text, tags_flat),
            )

    def get_meta(
        self,
        memory_id: str,
        *,
        entity_id: str = "",
        entity_type: str = "",
        folder: str = "facts",
        base_dir: str = "",
    ) -> Optional[Dict[str, Any]]:
        """获取单条记忆的 meta（按命名空间复合主键查询）。

        Schema 现在用 (entity_type, entity_id, folder, base_dir, id) 做
        PRIMARY KEY，所以必须传完整的 entity 上下文才能精确定位单行；只传
        裸 memory_id 会在跨实体重名时返回错记忆。
        """
        with self._read() as conn:
            row = conn.execute(
                "SELECT * FROM memories WHERE id = ? AND entity_id = ? AND "
                "entity_type = ? AND folder = ? AND base_dir = ?",
                (memory_id, entity_id, entity_type, folder, base_dir),
            ).fetchone()
        if row:
            return self._row_to_dict(row)
        return None

    def delete(
        self,
        memory_id: str,
        *,
        entity_id: str = "",
        entity_type: str = "",
        folder: str = "facts",
        base_dir: str = "",
    ):
        """删除记忆索引（按复合主键定位单行）。

        必须传完整 entity 上下文，否则跨实体重名时会一次删多条。FTS5 表用
        storage_key（复合 key 字符串）做单行定位。
        """
        storage_key = self._storage_key(
            memory_id, entity_id, entity_type, folder, base_dir
        )
        with self._transaction() as cur:
            cur.execute(
                "DELETE FROM memories WHERE id = ? AND entity_id = ? AND "
                "entity_type = ? AND folder = ? AND base_dir = ?",
                (memory_id, entity_id, entity_type, folder, base_dir),
            )
            cur.execute(
                "DELETE FROM memories_fts WHERE memory_id = ?", (storage_key,)
            )
            if self._vec_available:
                # memories_vec 主键还是单字段 id（vec0 限制）；如果将来要扩展
                # 跨实体向量搜索，可以把 vec0 的 id 也改成 storage_key。当前
                # 不会受撞名影响，因为向量是用 storage_key 作为 vec0 主键写入
                # 的（见 store_embedding 下方修改）。
                try:
                    cur.execute(
                        "DELETE FROM memories_vec WHERE id = ?", (storage_key,)
                    )
                except Exception:
                    pass

    def update_meta(
        self,
        memory_id: str,
        *,
        entity_id: str = "",
        entity_type: str = "",
        folder: str = "facts",
        base_dir: str = "",
        **kwargs,
    ):
        """部分更新 meta 字段（按复合主键单行）。"""
        allowed = {
            "importance", "last_accessed", "access_count",
            "tags", "source", "raw_text", "content_hash", "file_path",
        }
        updates = []
        values = []
        for k, v in kwargs.items():
            if k not in allowed:
                continue
            if k == "tags":
                v = json.dumps(v, ensure_ascii=False)
            elif k == "source":
                v = json.dumps(v, ensure_ascii=False)
            updates.append(f"{k} = ?")
            values.append(v)

        if not updates:
            return

        # WHERE 复合主键：避免一次更新多行（撞 ID 的跨实体记忆）
        values.extend([memory_id, entity_id, entity_type, folder, base_dir])
        needs_fts_sync = "raw_text" in kwargs or "tags" in kwargs

        storage_key = self._storage_key(
            memory_id, entity_id, entity_type, folder, base_dir
        )
        with self._transaction() as cur:
            cur.execute(
                f"UPDATE memories SET {', '.join(updates)} WHERE id = ? AND "
                "entity_id = ? AND entity_type = ? AND folder = ? AND base_dir = ?",
                values,
            )
            # 如果 raw_text 或 tags 变化，同步 FTS
            if needs_fts_sync:
                row = cur.execute(
                    "SELECT raw_text, tags FROM memories WHERE id = ? AND "
                    "entity_id = ? AND entity_type = ? AND folder = ? AND base_dir = ?",
                    (memory_id, entity_id, entity_type, folder, base_dir),
                ).fetchone()
                if row:
                    raw_text = row[0]
                    segmented = self._segment_for_fts(raw_text)
                    tags_list = json.loads(row[1]) if row[1] else []
                    tags_flat = " ".join(tags_list)
                    cur.execute(
                        "DELETE FROM memories_fts WHERE memory_id = ?",
                        (storage_key,),
                    )
                    cur.execute(
                        "INSERT INTO memories_fts(memory_id, raw_text, tags_text) "
                        "VALUES (?, ?, ?)",
                        (storage_key, segmented, tags_flat),
                    )

    def touch_access(
        self,
        memory_id: str,
        *,
        entity_id: str = "",
        entity_type: str = "",
        folder: str = "facts",
        base_dir: str = "",
    ):
        """标记一次访问: access_count +1, last_accessed = now（按复合主键）。"""
        now = time.time()
        with self._transaction() as cur:
            cur.execute(
                """UPDATE memories
                   SET access_count = access_count + 1,
                       last_accessed = ?
                   WHERE id = ? AND entity_id = ? AND entity_type = ?
                     AND folder = ? AND base_dir = ?""",
                (now, memory_id, entity_id, entity_type, folder, base_dir),
            )

    # ==========================================
    # 查询操作
    # ==========================================

    def list_memories(
        self,
        entity_id: str = "",
        entity_type: str = "",
        folder: str = "",
        base_dir: str = "",
        min_importance: int = 0,
        limit: int = 0,
    ) -> List[Dict[str, Any]]:
        """列出指定范围的记忆 meta"""
        conditions = []
        params = []

        if base_dir:
            conditions.append("base_dir = ?")
            params.append(base_dir)
            if folder:
                conditions.append("folder = ?")
                params.append(folder)
        else:
            if entity_id:
                conditions.append("entity_id = ?")
                params.append(entity_id)
            if entity_type:
                conditions.append("entity_type = ?")
                params.append(entity_type)
            if folder:
                conditions.append("folder = ?")
                params.append(folder)
            # 排除 global 域的记忆
            conditions.append("base_dir = ''")

        if min_importance > 0:
            conditions.append("importance >= ?")
            params.append(min_importance)

        where = " AND ".join(conditions) if conditions else "1=1"
        sql = f"SELECT * FROM memories WHERE {where} ORDER BY importance DESC"
        if limit > 0:
            sql += f" LIMIT {limit}"

        with self._read() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def count_memories(
        self,
        entity_id: str = "",
        entity_type: str = "",
        folder: str = "",
        base_dir: str = "",
    ) -> int:
        """统计记忆数量"""
        conditions = []
        params = []

        if base_dir:
            conditions.append("base_dir = ?")
            params.append(base_dir)
            if folder:
                conditions.append("folder = ?")
                params.append(folder)
        else:
            if entity_id:
                conditions.append("entity_id = ?")
                params.append(entity_id)
            if entity_type:
                conditions.append("entity_type = ?")
                params.append(entity_type)
            if folder:
                conditions.append("folder = ?")
                params.append(folder)
            conditions.append("base_dir = ''")

        where = " AND ".join(conditions) if conditions else "1=1"
        with self._read() as conn:
            row = conn.execute(
                f"SELECT COUNT(*) FROM memories WHERE {where}", params
            ).fetchone()
        return row[0] if row else 0

    # ==========================================
    # FTS5 全文检索
    # ==========================================

    def fts_search(
        self,
        query: str,
        entity_id: str = "",
        entity_type: str = "",
        folder: str = "",
        base_dir: str = "",
        k: int = 10,
    ) -> List[Dict[str, Any]]:
        """FTS5 全文检索

        使用 BM25 排序，结合 importance 和时间衰减做综合打分。
        """
        # 构造 FTS5 查询（处理中文：按字符拆分 + OR 连接）
        fts_query = self._build_fts_query(query)
        if not fts_query:
            return []

        # FTS5 搜索获取候选集
        try:
            fts_sql = """
                SELECT m.*, bm25(memories_fts) as fts_score
                FROM memories_fts fts
                JOIN memories m ON fts.memory_id = m.storage_key
                WHERE memories_fts MATCH ?
            """
            conditions = []
            params = [fts_query]

            if base_dir:
                conditions.append("m.base_dir = ?")
                params.append(base_dir)
                if folder:
                    conditions.append("m.folder = ?")
                    params.append(folder)
            else:
                if entity_id:
                    conditions.append("m.entity_id = ?")
                    params.append(entity_id)
                if entity_type:
                    conditions.append("m.entity_type = ?")
                    params.append(entity_type)
                if folder:
                    conditions.append("m.folder = ?")
                    params.append(folder)
                conditions.append("m.base_dir = ''")

            if conditions:
                fts_sql += " AND " + " AND ".join(conditions)

            fts_sql += " ORDER BY fts_score LIMIT ?"
            params.append(k * 3)  # 多取一些用于重排序

            with self._read() as conn:
                rows = conn.execute(fts_sql, params).fetchall()
        except sqlite3.OperationalError as e:
            logger.warning(f"FTS5 search error: {e}, query={fts_query}")
            return []

        if not rows:
            return []

        # 三维打分重排序
        now = time.time()
        scored = []
        for row in rows:
            d = self._row_to_dict(row)
            fts_score = abs(row["fts_score"])  # bm25() 返回负值

            imp = d["importance"] / 10.0
            days_since = max(0, (now - d["last_accessed"]) / 86400)
            time_decay = 0.5 ** (days_since / 30.0)

            # 综合分数
            final = fts_score * (1.0 + imp * 0.3 + time_decay * 0.2)
            d["_score"] = final
            scored.append(d)

        scored.sort(key=lambda x: x["_score"], reverse=True)
        return scored[:k]

    # ==========================================
    # 向量检索（优雅降级）
    # ==========================================

    def hybrid_search(
        self,
        query: str,
        query_embedding: list = None,
        entity_id: str = "",
        entity_type: str = "",
        folder: str = "",
        base_dir: str = "",
        k: int = 5,
        vector_weight: float = 0.7,
        fts_weight: float = 0.3,
    ) -> List[Dict[str, Any]]:
        """混合检索：向量 + FTS5（OpenClaw 风格优雅降级）

        降级策略:
        1. 有 embedding 且 vec 可用 → 混合检索 (vector_weight + fts_weight)
        2. 有 embedding 但 vec 不可用 → 纯 FTS5
        3. 无 embedding → 纯 FTS5
        """
        fts_results = self.fts_search(
            query, entity_id, entity_type, folder, base_dir, k=k * 2
        )

        # 如果向量检索不可用，直接返回 FTS 结果
        if not self._vec_available or not query_embedding:
            return fts_results[:k]

        # 向量检索
        vec_results = self._vec_search(
            query_embedding, entity_id, entity_type, folder, base_dir, k=k * 2
        )

        if not vec_results:
            return fts_results[:k]

        # 混合打分：归一化后加权合并
        return self._merge_results(
            fts_results, vec_results,
            fts_weight=fts_weight,
            vec_weight=vector_weight,
            k=k,
        )

    def _vec_search(
        self,
        embedding: list,
        entity_id: str = "",
        entity_type: str = "",
        folder: str = "",
        base_dir: str = "",
        k: int = 10,
    ) -> List[Dict[str, Any]]:
        """纯向量检索"""
        if not self._vec_available:
            return []

        try:
            # sqlite-vec 距离查询
            import json as _json
            emb_json = _json.dumps(embedding)

            # sqlite-vec 的 vec0 虚拟表无法把 JOIN 的 WHERE 条件下推到 KNN
            # 计算阶段，所以 entity_id/entity_type/folder 的过滤只能事后做。
            # 如果别的 entity 占了向量库的多数，预取 k*3 过完滤可能所剩无几。
            # 抬高预取倍数确保过滤后还有充足候选；上限设 200，避免 k 极大时
            # 把库扫空。如果未来切到 vec0 metadata 列做 KNN 阶段过滤，可以
            # 把这里的倍数调回 3。
            prefetch_k = max(k * 10, 50)
            if prefetch_k > 200:
                prefetch_k = 200
            with self._read() as conn:
                rows = conn.execute("""
                    SELECT v.id, v.distance, m.*
                    FROM memories_vec v
                    JOIN memories m ON v.id = m.storage_key
                    WHERE v.embedding MATCH ?
                    AND k = ?
                """, (emb_json, prefetch_k)).fetchall()

            results = []
            for row in rows:
                d = self._row_to_dict(row)
                # 距离转相似度（余弦距离）
                d["_vec_score"] = 1.0 / (1.0 + row["distance"])
                results.append(d)

            # 过滤 entity 范围 —— 必须同时包含 base_dir，否则 global/self
            # 这类按 base_dir 划分命名空间的搜索路径会把别的命名空间结果混进来。
            if entity_id or entity_type or folder or base_dir:
                results = [
                    r for r in results
                    if (not entity_id or r["entity_id"] == entity_id)
                    and (not entity_type or r["entity_type"] == entity_type)
                    and (not folder or r["folder"] == folder)
                    and (not base_dir or r.get("base_dir", "") == base_dir)
                ]

            return results
        except Exception as e:
            logger.warning(f"Vector search error: {e}")
            return []

    def store_embedding(
        self,
        memory_id: str,
        embedding: list,
        *,
        entity_id: str = "",
        entity_type: str = "",
        folder: str = "facts",
        base_dir: str = "",
    ):
        """存储向量嵌入（如果 vec 可用）；vec0 表用 storage_key 做主键。"""
        if not self._vec_available:
            return

        try:
            emb_json = json.dumps(embedding)
            storage_key = self._storage_key(
                memory_id, entity_id, entity_type, folder, base_dir
            )
            with self._transaction() as cur:
                cur.execute(
                    "INSERT OR REPLACE INTO memories_vec(id, embedding) VALUES (?, ?)",
                    (storage_key, emb_json),
                )
        except Exception as e:
            logger.warning(f"Store embedding error: {e}")

    def needs_embedding(
        self,
        memory_id: str,
        content_hash: str,
        *,
        entity_id: str = "",
        entity_type: str = "",
        folder: str = "facts",
        base_dir: str = "",
    ) -> bool:
        """检查是否需要（重新）生成嵌入

        OpenClaw 策略: 通过 content_hash 判断内容是否变化
        """
        if not self._vec_available:
            return False

        storage_key = self._storage_key(
            memory_id, entity_id, entity_type, folder, base_dir
        )
        with self._read() as conn:
            # 检查是否已有嵌入
            row = conn.execute(
                "SELECT content_hash FROM memories WHERE id = ? AND entity_id = ? "
                "AND entity_type = ? AND folder = ? AND base_dir = ?",
                (memory_id, entity_id, entity_type, folder, base_dir),
            ).fetchone()

            if not row:
                return True

            # 检查 hash 是否变化
            if row["content_hash"] != content_hash:
                return True

            # 检查向量表中是否有此 id
            vec_row = conn.execute(
                "SELECT id FROM memories_vec WHERE id = ?", (storage_key,)
            ).fetchone()

        return vec_row is None

    # ==========================================
    # 去重辅助
    # ==========================================

    def find_by_hash(
        self,
        content_hash: str,
        entity_id: str = "",
        entity_type: str = "",
        folder: str = "",
        base_dir: str = "",
    ) -> Optional[Dict[str, Any]]:
        """通过内容 hash 快速查找精确重复（按完整命名空间过滤）。

        `base_dir` 同样是命名空间维度——比如 `global/self`、`global/skills`
        下的记忆和某个 user/group 的记忆完全不能算同一条，即使内容 hash
        相同。漏掉这个维度会让 dedup / merge 跨命名空间串线。
        """
        conditions = ["content_hash = ?"]
        params: list = [content_hash]

        if entity_id:
            conditions.append("entity_id = ?")
            params.append(entity_id)
        if entity_type:
            conditions.append("entity_type = ?")
            params.append(entity_type)
        if folder:
            conditions.append("folder = ?")
            params.append(folder)
        # base_dir 显式参与命名空间隔离：传值时按值过滤，未传时默认锁定
        # 普通 entity 域（base_dir = ''），避免 global 域记忆误命中。
        conditions.append("base_dir = ?")
        params.append(base_dir or "")

        where = " AND ".join(conditions)
        with self._read() as conn:
            row = conn.execute(
                f"SELECT * FROM memories WHERE {where} LIMIT 1", params
            ).fetchone()

        if row:
            return self._row_to_dict(row)
        return None

    # ==========================================
    # 批量操作
    # ==========================================

    def bulk_upsert(self, records: List[Dict[str, Any]]):
        """批量插入/更新（用于初始化索引重建）"""
        with self._transaction() as cur:
            for rec in records:
                mem_id = rec.get("id", "")
                entity_id = rec.get("entity_id", "")
                entity_type = rec.get("entity_type", "")
                folder = rec.get("folder", "facts")
                base_dir = rec.get("base_dir", "")
                tags = rec.get("tags", [])
                tags_json = json.dumps(tags, ensure_ascii=False)
                source_json = json.dumps(rec.get("source", {}), ensure_ascii=False)
                raw_text = rec.get("raw_text", "")
                chash = self.content_hash(raw_text)
                tags_flat = " ".join(tags)

                cur.execute("""
                    INSERT INTO memories
                        (id, entity_id, entity_type, folder, memory_type,
                         importance, timestamp, last_accessed, access_count,
                         tags, source, content_hash, file_path, base_dir, raw_text)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(entity_type, entity_id, folder, base_dir, id) DO UPDATE SET
                        importance = excluded.importance,
                        last_accessed = excluded.last_accessed,
                        access_count = excluded.access_count,
                        tags = excluded.tags,
                        content_hash = excluded.content_hash,
                        raw_text = excluded.raw_text
                """, (
                    mem_id,
                    entity_id,
                    entity_type,
                    folder,
                    rec.get("memory_type", "fact"),
                    rec.get("importance", 5),
                    rec.get("timestamp", time.time()),
                    rec.get("last_accessed", time.time()),
                    rec.get("access_count", 0),
                    tags_json, source_json, chash,
                    rec.get("file_path", ""),
                    base_dir,
                    raw_text,
                ))

                # 同步 FTS（用分词后的文本）；用复合 storage_key 做 FTS row 键
                storage_key = self._storage_key(
                    mem_id, entity_id, entity_type, folder, base_dir
                )
                segmented = self._segment_for_fts(raw_text)
                cur.execute(
                    "DELETE FROM memories_fts WHERE memory_id = ?", (storage_key,)
                )
                cur.execute(
                    "INSERT INTO memories_fts(memory_id, raw_text, tags_text) "
                    "VALUES (?, ?, ?)",
                    (storage_key, segmented, tags_flat),
                )

    def rebuild_index_from_files(self, scan_dir: str):
        """从文件系统重建索引（灾难恢复）

        扫描 `scan_dir` 下所有 TOML（优先）和 JSON 文件（旧格式兼容），重新
        喂给 SQLite 索引。

        **`archive/` 目录被显式排除**——里面的记忆是「已归档/已删除」，
        rebuild 不应该把它们复活到主索引里。

        **重建前先清空 `memories` / `memories_fts`**——`bulk_upsert` 只能
        覆盖同 id 的旧行，不会清理那些真相源文件已经删除、但索引里还残留
        的「僵尸行」。先 TRUNCATE 再灌入扫到的记录，才是真正的 rebuild
        语义。
        """
        import glob

        try:
            import tomllib  # Python 3.11+
        except ImportError:
            import tomli as tomllib  # Python 3.10 fallback

        scan_dir_norm = os.path.normpath(scan_dir)
        archive_root = os.path.normpath(os.path.join(scan_dir_norm, "archive"))
        archive_prefix = archive_root + os.sep

        def _is_archived(fpath: str) -> bool:
            """判断文件是否落在 scan_dir/archive/ 下。"""
            try:
                p = os.path.normpath(fpath)
            except Exception:
                return False
            return p == archive_root or p.startswith(archive_prefix)

        records = []

        # 优先扫描 TOML 文件
        for fpath in glob.glob(os.path.join(scan_dir, "**", "*.toml"), recursive=True):
            if _is_archived(fpath):
                continue
            try:
                with open(fpath, "rb") as f:
                    data = tomllib.load(f)

                entity_id, entity_type, folder, base_dir = self._parse_path(fpath, scan_dir)

                rec = {
                    "id": data.get("id", os.path.basename(fpath)[:-5]),
                    "entity_id": entity_id,
                    "entity_type": entity_type,
                    "folder": folder,
                    "base_dir": base_dir,
                    "memory_type": data.get("type", "fact"),
                    "raw_text": data.get("text", ""),
                    "importance": data.get("importance", 5),
                    "tags": data.get("tags", []),
                    "source": data.get("source", {}),
                    "file_path": fpath,
                }

                # 归档文件可能含 meta
                meta = data.get("meta", {})
                if meta:
                    rec["timestamp"] = meta.get("timestamp", 0)
                    rec["last_accessed"] = meta.get("last_accessed", 0)
                    rec["access_count"] = meta.get("access_count", 0)

                if rec["id"]:
                    records.append(rec)
            except Exception as e:
                logger.warning(f"Failed to parse TOML {fpath}: {e}")

        # 兼容旧 JSON 文件
        for fpath in glob.glob(os.path.join(scan_dir, "**", "*.json"), recursive=True):
            if "profile.json" in fpath or "chat_memory" in fpath:
                continue
            if _is_archived(fpath):
                continue
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    data = json.load(f)

                entity_id, entity_type, folder, base_dir = self._parse_path(fpath, scan_dir)

                rec = {
                    "id": data.get("id", ""),
                    "entity_id": entity_id,
                    "entity_type": entity_type,
                    "folder": folder,
                    "base_dir": base_dir,
                    "memory_type": data.get("type", "fact"),
                    "raw_text": data.get("content", {}).get("raw_text", ""),
                    "file_path": fpath,
                }

                meta = data.get("meta", {})
                if meta:
                    rec["importance"] = meta.get("importance", 5)
                    rec["timestamp"] = meta.get("timestamp", 0)
                    rec["last_accessed"] = meta.get("last_accessed", 0)
                    rec["access_count"] = meta.get("access_count", 0)
                    rec["tags"] = meta.get("tags", [])
                    rec["source"] = meta.get("source", {})

                if rec["id"]:
                    records.append(rec)
            except Exception as e:
                logger.warning(f"Failed to parse JSON {fpath}: {e}")

        # 先 TRUNCATE 主表和 FTS 影子表，避免「真相源已删但索引残留」的僵尸行。
        with self._transaction() as cur:
            cur.execute("DELETE FROM memories")
            cur.execute("DELETE FROM memories_fts")
            # 注意：memories_vec 由 ANN 索引维护，重建索引时一并清掉，让
            # 后续访问按需重新 embed。如果 sqlite-vec 未加载会抛错，安全忽略。
            try:
                cur.execute("DELETE FROM memories_vec")
            except sqlite3.OperationalError:
                pass

        if records:
            self.bulk_upsert(records)
            logger.info(
                f"Rebuilt index from {len(records)} files (archive/ excluded)"
            )
        else:
            logger.info("Rebuilt index — 0 records (archive/ excluded)")

    @staticmethod
    def _parse_path(
        fpath: str, base_scan_dir: str
    ) -> Tuple[str, str, str, str]:
        """从文件路径解析 entity 信息

        Returns:
            (entity_id, entity_type, folder, base_dir)

        `base_dir` 用于把 `global/self`、`global/facts` 这类按命名空间划分
        的文件区分开——之前直接丢空，rebuild 后混合检索的 base_dir 过滤就
        把它们当成普通域，与真正的 user 数据混到一起。
        """
        rel = os.path.relpath(fpath, base_scan_dir)
        parts = rel.replace("\\", "/").split("/")

        entity_id = ""
        entity_type = ""
        folder = "facts"
        base_dir = ""

        # entities/{type}_{quoted_id}/{folder}/{mem_id}.toml
        if len(parts) >= 3 and parts[0] == "entities":
            dirname = parts[1]
            for et in ("user", "group", "channel"):
                prefix = f"{et}_"
                if dirname.startswith(prefix):
                    entity_type = et
                    entity_id = _path_segment_to_id(dirname[len(prefix):])
                    break
            folder = parts[2] if len(parts) >= 3 else "facts"
        # global/{...} 各种命名空间——把 global 根之下的子段保留进 base_dir
        # 让查询能按命名空间隔离。
        elif len(parts) >= 2 and parts[0] == "global":
            # global/self/{folder}/{mem_id}.toml → base_dir = "global/self"
            # global/facts/{mem_id}.toml         → base_dir = "global"
            # global/skills/{...}                → base_dir = "global/skills"
            if len(parts) >= 4 and parts[1] in ("self", "skills"):
                base_dir = f"global/{parts[1]}"
                folder = parts[2]
            else:
                base_dir = "global"
                folder = parts[-2] if len(parts) >= 2 else "facts"

        return entity_id, entity_type, folder, base_dir

    # ==========================================
    # 内部工具
    # ==========================================

    @staticmethod
    def _build_fts_query(text: str) -> str:
        """构造 FTS5 查询字符串

        FTS 表中存储的是 jieba 分词后的文本，
        查询也用 jieba 分词后用 OR 连接各 token。
        """
        if not text or not text.strip():
            return ""

        # 清理 FTS5 特殊字符
        cleaned = text.strip()
        for ch in ['"', "'", "(", ")", "*", "+", "-", ":", "^", "{", "}", "~",
                   "[", "]", "@", "<", ">", "/", "\\", "|", "!", "?", "#", "&",
                   "=", ";", ",", "."]:
            cleaned = cleaned.replace(ch, " ")
        cleaned = cleaned.strip()
        if not cleaned:
            return ""

        # jieba 分词（可选；未安装时降级到字符级 fallback）
        if _JIEBA_AVAILABLE:
            tokens = [t.strip() for t in jieba.lcut(cleaned) if t.strip()]
        else:
            tokens = [t.strip() for t in _fallback_tokenize(cleaned) if t.strip()]

        # 过滤单字符（中文停用词）但保留英文单字符
        tokens = [t for t in tokens if len(t) > 1 or t.isascii()]
        if not tokens:
            return f"{cleaned}"

        if len(tokens) == 1:
            return tokens[0]

        # OR 连接各 token
        return " OR ".join(tokens)

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
        """sqlite3.Row → dict，反序列化 JSON 字段"""
        d = dict(row)
        # 反序列化 JSON 字段
        for key in ("tags", "source"):
            if key in d and isinstance(d[key], str):
                try:
                    d[key] = json.loads(d[key])
                except (json.JSONDecodeError, TypeError):
                    d[key] = [] if key == "tags" else {}
        return d

    @staticmethod
    def _merge_results(
        fts_results: List[Dict],
        vec_results: List[Dict],
        fts_weight: float = 0.3,
        vec_weight: float = 0.7,
        k: int = 5,
    ) -> List[Dict[str, Any]]:
        """合并 FTS 和向量检索结果（RRF-style）"""
        # 归一化 FTS 分数
        fts_scores = {}
        if fts_results:
            max_fts = max(r.get("_score", 0) for r in fts_results) or 1.0
            for r in fts_results:
                fts_scores[r["id"]] = r.get("_score", 0) / max_fts

        # 归一化 vec 分数
        vec_scores = {}
        if vec_results:
            max_vec = max(r.get("_vec_score", 0) for r in vec_results) or 1.0
            for r in vec_results:
                vec_scores[r["id"]] = r.get("_vec_score", 0) / max_vec

        # 合并所有候选
        all_ids = set(fts_scores.keys()) | set(vec_scores.keys())
        merged = []

        # 建立 id → record 映射
        records_map = {}
        for r in fts_results + vec_results:
            if r["id"] not in records_map:
                records_map[r["id"]] = r

        for mid in all_ids:
            fs = fts_scores.get(mid, 0)
            vs = vec_scores.get(mid, 0)
            final = fts_weight * fs + vec_weight * vs
            rec = records_map[mid].copy()
            rec["_score"] = final
            merged.append(rec)

        merged.sort(key=lambda x: x["_score"], reverse=True)
        return merged[:k]

    # ==========================================
    # 生命周期
    # ==========================================

    def close(self):
        """关闭数据库连接（持锁等所有 in-flight SQL 跑完再 close）。"""
        with self._conn_lock:
            if self._conn:
                self._conn.close()
                self._conn = None

    def __del__(self):
        self.close()
