"""
SQLite-backed memory index — MemoryIndex

All memory metadata (importance, timestamps, tags, access_count, ...)
lives in SQLite. TOML files on disk hold the ground-truth content;
this index is rebuildable from them.

Capabilities:
  - FTS5 full-text search (jieba-tokenized so Chinese is searchable)
  - Structured meta queries (importance, time range, tags)
  - SHA-256 content hash for exact-duplicate detection
  - Optional vector embeddings (sqlite-vec) + hybrid search with
    graceful degradation when the extension is unavailable
"""

import hashlib
import json
import os
import sqlite3
import time
from contextlib import contextmanager
from typing import Any, Dict, List, Optional, Tuple

import jieba

from core.logging_manager import get_logger
from core.utils.path_utils import get_data_path

logger = get_logger("kiraos_memory_index", "green")

DEFAULT_DB_PATH = str(get_data_path() / "memory" / "memory_index.db")


class MemoryIndex:
    """SQLite-persisted memory index.

    Responsibilities:
      - Persist meta for every memory
      - FTS5 full-text search
      - Optional vector hybrid search (graceful degrade to pure FTS5)
      - SHA-256 dedup
    """

    def __init__(self, db_path: str = ""):
        self.db_path = db_path or DEFAULT_DB_PATH
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._conn: Optional[sqlite3.Connection] = None
        self._vec_available = False
        self._embedder = None
        self._init_db()

    def _init_db(self):
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")

        with self._transaction() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS memories (
                    id TEXT PRIMARY KEY,
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
                    raw_text TEXT NOT NULL DEFAULT ''
                )
            """)

            cur.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
                    memory_id UNINDEXED,
                    raw_text,
                    tags_text,
                    tokenize='unicode61'
                )
            """)

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

        self._try_init_vec()

        logger.info(
            f"MemoryIndex initialized: db={self.db_path}, "
            f"vec_available={self._vec_available}"
        )

    def _try_init_vec(self):
        """Load the sqlite-vec extension when present; degrade silently otherwise."""
        try:
            import sqlite_vec  # noqa: F401
            self._conn.enable_load_extension(True)
            sqlite_vec.load(self._conn)
            self._conn.enable_load_extension(False)

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
        cur = self._conn.cursor()
        try:
            yield cur
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        finally:
            cur.close()

    @staticmethod
    def content_hash(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def _segment_for_fts(text: str) -> str:
        """jieba-tokenize for FTS5 unicode61 so Chinese is searchable."""
        if not text:
            return ""
        tokens = [t for t in jieba.lcut(text) if t.strip()]
        return " ".join(tokens)

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
        now = time.time()
        if not timestamp:
            timestamp = now
        if not last_accessed:
            last_accessed = now

        tags_json = json.dumps(tags or [], ensure_ascii=False)
        source_json = json.dumps(source or {}, ensure_ascii=False)
        chash = self.content_hash(raw_text)

        segmented_text = self._segment_for_fts(raw_text)
        tags_flat = " ".join(tags or [])

        with self._transaction() as cur:
            cur.execute("""
                INSERT INTO memories
                    (id, entity_id, entity_type, folder, memory_type,
                     importance, timestamp, last_accessed, access_count,
                     tags, source, content_hash, file_path, base_dir, raw_text)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
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

            cur.execute(
                "DELETE FROM memories_fts WHERE memory_id = ?", (memory_id,)
            )
            cur.execute(
                "INSERT INTO memories_fts(memory_id, raw_text, tags_text) VALUES (?, ?, ?)",
                (memory_id, segmented_text, tags_flat),
            )

    def get_meta(self, memory_id: str) -> Optional[Dict[str, Any]]:
        row = self._conn.execute(
            "SELECT * FROM memories WHERE id = ?", (memory_id,)
        ).fetchone()
        if row:
            return self._row_to_dict(row)
        return None

    def delete(self, memory_id: str):
        with self._transaction() as cur:
            cur.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
            cur.execute(
                "DELETE FROM memories_fts WHERE memory_id = ?", (memory_id,)
            )
            if self._vec_available:
                try:
                    cur.execute(
                        "DELETE FROM memories_vec WHERE id = ?", (memory_id,)
                    )
                except Exception:
                    pass

    def update_meta(self, memory_id: str, **kwargs):
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

        values.append(memory_id)
        needs_fts_sync = "raw_text" in kwargs or "tags" in kwargs

        with self._transaction() as cur:
            cur.execute(
                f"UPDATE memories SET {', '.join(updates)} WHERE id = ?",
                values,
            )
            if needs_fts_sync:
                row = cur.execute(
                    "SELECT raw_text, tags FROM memories WHERE id = ?",
                    (memory_id,),
                ).fetchone()
                if row:
                    raw_text = row[0]
                    segmented = self._segment_for_fts(raw_text)
                    tags_list = json.loads(row[1]) if row[1] else []
                    tags_flat = " ".join(tags_list)
                    cur.execute(
                        "DELETE FROM memories_fts WHERE memory_id = ?",
                        (memory_id,),
                    )
                    cur.execute(
                        "INSERT INTO memories_fts(memory_id, raw_text, tags_text) "
                        "VALUES (?, ?, ?)",
                        (memory_id, segmented, tags_flat),
                    )

    def touch_access(self, memory_id: str):
        now = time.time()
        with self._transaction() as cur:
            cur.execute(
                """UPDATE memories
                   SET access_count = access_count + 1,
                       last_accessed = ?
                   WHERE id = ?""",
                (now, memory_id),
            )

    def list_memories(
        self,
        entity_id: str = "",
        entity_type: str = "",
        folder: str = "",
        base_dir: str = "",
        min_importance: int = 0,
        limit: int = 0,
    ) -> List[Dict[str, Any]]:
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

        if min_importance > 0:
            conditions.append("importance >= ?")
            params.append(min_importance)

        where = " AND ".join(conditions) if conditions else "1=1"
        sql = f"SELECT * FROM memories WHERE {where} ORDER BY importance DESC"
        if limit > 0:
            sql += f" LIMIT {limit}"

        rows = self._conn.execute(sql, params).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def count_memories(
        self,
        entity_id: str = "",
        entity_type: str = "",
        folder: str = "",
        base_dir: str = "",
    ) -> int:
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
        row = self._conn.execute(
            f"SELECT COUNT(*) FROM memories WHERE {where}", params
        ).fetchone()
        return row[0] if row else 0

    def fts_search(
        self,
        query: str,
        entity_id: str = "",
        entity_type: str = "",
        folder: str = "",
        base_dir: str = "",
        k: int = 10,
    ) -> List[Dict[str, Any]]:
        """FTS5 full-text search with BM25 + importance + time-decay re-ranking."""
        fts_query = self._build_fts_query(query)
        if not fts_query:
            return []

        try:
            fts_sql = """
                SELECT m.*, bm25(memories_fts) as fts_score
                FROM memories_fts fts
                JOIN memories m ON fts.memory_id = m.id
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
            params.append(k * 3)

            rows = self._conn.execute(fts_sql, params).fetchall()
        except sqlite3.OperationalError as e:
            logger.warning(f"FTS5 search error: {e}, query={fts_query}")
            return []

        if not rows:
            return []

        now = time.time()
        scored = []
        for row in rows:
            d = self._row_to_dict(row)
            fts_score = abs(row["fts_score"])  # bm25() returns negative

            imp = d["importance"] / 10.0
            days_since = max(0, (now - d["last_accessed"]) / 86400)
            time_decay = 0.5 ** (days_since / 30.0)

            final = fts_score * (1.0 + imp * 0.3 + time_decay * 0.2)
            d["_score"] = final
            scored.append(d)

        scored.sort(key=lambda x: x["_score"], reverse=True)
        return scored[:k]

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
        """Hybrid retrieval: vector + FTS5 with graceful degradation."""
        fts_results = self.fts_search(
            query, entity_id, entity_type, folder, base_dir, k=k * 2
        )

        if not self._vec_available or not query_embedding:
            return fts_results[:k]

        vec_results = self._vec_search(
            query_embedding, entity_id, entity_type, folder, base_dir, k=k * 2
        )

        if not vec_results:
            return fts_results[:k]

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
        if not self._vec_available:
            return []

        try:
            import json as _json
            emb_json = _json.dumps(embedding)

            rows = self._conn.execute("""
                SELECT v.id, v.distance, m.*
                FROM memories_vec v
                JOIN memories m ON v.id = m.id
                WHERE v.embedding MATCH ?
                AND k = ?
            """, (emb_json, k * 3)).fetchall()

            results = []
            for row in rows:
                d = self._row_to_dict(row)
                d["_vec_score"] = 1.0 / (1.0 + row["distance"])
                results.append(d)

            if entity_id or entity_type or folder:
                results = [
                    r for r in results
                    if (not entity_id or r["entity_id"] == entity_id)
                    and (not entity_type or r["entity_type"] == entity_type)
                    and (not folder or r["folder"] == folder)
                ]

            return results
        except Exception as e:
            logger.warning(f"Vector search error: {e}")
            return []

    def store_embedding(self, memory_id: str, embedding: list):
        if not self._vec_available:
            return

        try:
            emb_json = json.dumps(embedding)
            with self._transaction() as cur:
                cur.execute(
                    "INSERT OR REPLACE INTO memories_vec(id, embedding) VALUES (?, ?)",
                    (memory_id, emb_json),
                )
        except Exception as e:
            logger.warning(f"Store embedding error: {e}")

    def needs_embedding(self, memory_id: str, content_hash: str) -> bool:
        if not self._vec_available:
            return False

        row = self._conn.execute(
            "SELECT content_hash FROM memories WHERE id = ?", (memory_id,)
        ).fetchone()

        if not row:
            return True

        if row["content_hash"] != content_hash:
            return True

        vec_row = self._conn.execute(
            "SELECT id FROM memories_vec WHERE id = ?", (memory_id,)
        ).fetchone()

        return vec_row is None

    def find_by_hash(
        self,
        content_hash: str,
        entity_id: str = "",
        entity_type: str = "",
        folder: str = "",
    ) -> Optional[Dict[str, Any]]:
        conditions = ["content_hash = ?"]
        params = [content_hash]

        if entity_id:
            conditions.append("entity_id = ?")
            params.append(entity_id)
        if entity_type:
            conditions.append("entity_type = ?")
            params.append(entity_type)
        if folder:
            conditions.append("folder = ?")
            params.append(folder)

        where = " AND ".join(conditions)
        row = self._conn.execute(
            f"SELECT * FROM memories WHERE {where} LIMIT 1", params
        ).fetchone()

        if row:
            return self._row_to_dict(row)
        return None

    def bulk_upsert(self, records: List[Dict[str, Any]]):
        with self._transaction() as cur:
            for rec in records:
                mem_id = rec.get("id", "")
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
                    ON CONFLICT(id) DO UPDATE SET
                        importance = excluded.importance,
                        last_accessed = excluded.last_accessed,
                        access_count = excluded.access_count,
                        tags = excluded.tags,
                        content_hash = excluded.content_hash,
                        raw_text = excluded.raw_text
                """, (
                    mem_id,
                    rec.get("entity_id", ""),
                    rec.get("entity_type", ""),
                    rec.get("folder", "facts"),
                    rec.get("memory_type", "fact"),
                    rec.get("importance", 5),
                    rec.get("timestamp", time.time()),
                    rec.get("last_accessed", time.time()),
                    rec.get("access_count", 0),
                    tags_json, source_json, chash,
                    rec.get("file_path", ""),
                    rec.get("base_dir", ""),
                    raw_text,
                ))

                segmented = self._segment_for_fts(raw_text)
                cur.execute(
                    "DELETE FROM memories_fts WHERE memory_id = ?", (mem_id,)
                )
                cur.execute(
                    "INSERT INTO memories_fts(memory_id, raw_text, tags_text) "
                    "VALUES (?, ?, ?)",
                    (mem_id, segmented, tags_flat),
                )

    def rebuild_index_from_files(self, scan_dir: str):
        """Disaster-recovery: rebuild SQLite index by scanning TOML/JSON files."""
        import glob

        try:
            import tomllib  # Python 3.11+
        except ImportError:
            import tomli as tomllib  # Python 3.10 fallback

        records = []

        for fpath in glob.glob(os.path.join(scan_dir, "**", "*.toml"), recursive=True):
            try:
                with open(fpath, "rb") as f:
                    data = tomllib.load(f)

                entity_id, entity_type, folder = self._parse_path(fpath, scan_dir)

                rec = {
                    "id": data.get("id", os.path.basename(fpath)[:-5]),
                    "entity_id": entity_id,
                    "entity_type": entity_type,
                    "folder": folder,
                    "memory_type": data.get("type", "fact"),
                    "raw_text": data.get("text", ""),
                    "importance": data.get("importance", 5),
                    "tags": data.get("tags", []),
                    "source": data.get("source", {}),
                    "file_path": fpath,
                }

                meta = data.get("meta", {})
                if meta:
                    rec["timestamp"] = meta.get("timestamp", 0)
                    rec["last_accessed"] = meta.get("last_accessed", 0)
                    rec["access_count"] = meta.get("access_count", 0)

                if rec["id"]:
                    records.append(rec)
            except Exception as e:
                logger.warning(f"Failed to parse TOML {fpath}: {e}")

        # Legacy JSON compatibility
        for fpath in glob.glob(os.path.join(scan_dir, "**", "*.json"), recursive=True):
            if "profile.json" in fpath or "chat_memory" in fpath:
                continue
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    data = json.load(f)

                entity_id, entity_type, folder = self._parse_path(fpath, scan_dir)

                rec = {
                    "id": data.get("id", ""),
                    "entity_id": entity_id,
                    "entity_type": entity_type,
                    "folder": folder,
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

        if records:
            self.bulk_upsert(records)
            logger.info(f"Rebuilt index from {len(records)} files")

    @staticmethod
    def _parse_path(
        fpath: str, base_scan_dir: str
    ) -> Tuple[str, str, str]:
        rel = os.path.relpath(fpath, base_scan_dir)
        parts = rel.replace("\\", "/").split("/")

        entity_id = ""
        entity_type = ""
        folder = "facts"

        if len(parts) >= 3 and parts[0] == "entities":
            dirname = parts[1]
            for et in ("user", "group", "channel"):
                prefix = f"{et}_"
                if dirname.startswith(prefix):
                    entity_type = et
                    entity_id = dirname[len(prefix):]
                    break
            folder = parts[2] if len(parts) >= 3 else "facts"
        elif len(parts) >= 2 and parts[0] == "global":
            entity_type = ""
            entity_id = ""
            folder = parts[-2] if len(parts) >= 2 else ""

        return entity_id, entity_type, folder

    @staticmethod
    def _build_fts_query(text: str) -> str:
        if not text or not text.strip():
            return ""

        cleaned = text.strip()
        for ch in ['"', "'", "(", ")", "*", "+", "-", ":", "^", "{", "}", "~",
                   "[", "]", "@", "<", ">", "/", "\\", "|", "!", "?", "#", "&",
                   "=", ";", ",", "."]:
            cleaned = cleaned.replace(ch, " ")
        cleaned = cleaned.strip()
        if not cleaned:
            return ""

        tokens = [t.strip() for t in jieba.lcut(cleaned) if t.strip()]
        tokens = [t for t in tokens if len(t) > 1 or t.isascii()]
        if not tokens:
            return f"{cleaned}"

        if len(tokens) == 1:
            return tokens[0]

        return " OR ".join(tokens)

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
        d = dict(row)
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
        fts_scores = {}
        if fts_results:
            max_fts = max(r.get("_score", 0) for r in fts_results) or 1.0
            for r in fts_results:
                fts_scores[r["id"]] = r.get("_score", 0) / max_fts

        vec_scores = {}
        if vec_results:
            max_vec = max(r.get("_vec_score", 0) for r in vec_results) or 1.0
            for r in vec_results:
                vec_scores[r["id"]] = r.get("_vec_score", 0) / max_vec

        all_ids = set(fts_scores.keys()) | set(vec_scores.keys())
        merged = []

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

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None

    def __del__(self):
        self.close()
