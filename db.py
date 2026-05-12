"""
SQLite-based user memory storage for KiraAI.

Tables:
  - user_profiles: Long-term key-value user profile entries
                   with confidence, category, and optional expiration
  - event_logs:    Recent event logs per user

Concurrency model:
  - One SQLite connection **per thread** (thread-local), all opened against the
    same WAL-mode database file. WAL allows concurrent readers + a single writer.
  - Reads are lock-free (SQLite handles MVCC under WAL).
  - Writes serialize on a lightweight asyncio-friendly Lock.
  - Connections are tracked in a registry so close() can shut them all down.

Expiration:
  - `expires_at` is stored as INTEGER (unix epoch seconds) so comparisons can
    use a btree-friendly `?` parameter rather than the SQLite `datetime()`
    function which forces a full scan.
"""

import hashlib
import json
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from threading import Lock
from typing import Dict, List, Optional, Tuple

from core.logging_manager import get_logger

logger = get_logger("kiraos_db", "green")


def _mask_id(value: object) -> str:
    """Return a masked identifier safe for log files.

    Mirrors the helper in ``web_server.py`` so user_ids and memory keys never
    leak into rotating log files (which may be retained, shared, or pasted
    into bug reports). Format: ``<3-char prefix>***(<8-char sha256>)``.
    """
    s = "" if value is None else str(value)
    if not s:
        return "<empty>"
    digest = hashlib.sha256(s.encode("utf-8", errors="replace")).hexdigest()[:8]
    prefix = s[:3] if len(s) >= 3 else s
    return f"{prefix}***({digest})"

# Category priority for context injection (lower = higher priority)
CATEGORY_PRIORITY = {"basic": 0, "preference": 1, "social": 2, "other": 3}
VALID_CATEGORIES = set(CATEGORY_PRIORITY.keys())

# Hard caps to keep a single LLM-driven write from polluting the database.
# Profile values must be short enough that the LLM cannot stash a whole
# conversation summary as one entry; events get a more generous budget since
# they're already capped by max_event_keep.
MAX_PROFILE_VALUE_LEN = 500
MAX_EVENT_LEN = 1000

# Pre-flight conflict semantics:
# A new value differs from the existing one. Reject (return "conflict") iff
# the existing entry is at least this much more confident than the proposed
# one — that protects high-confidence facts from being silently overwritten
# by a low-confidence guess. The LLM may retry with force=True or supply a
# higher confidence to override.
CONFLICT_CONFIDENCE_GAP = 0.2


def _parse_ttl(ttl: str) -> Optional[datetime]:
    """Parse a TTL string like '30d', '7d', '12h', '30m' into an expiration datetime."""
    m = re.fullmatch(r"(\d+)\s*([dhm])", ttl.strip().lower())
    if not m:
        return None
    amount, unit = int(m.group(1)), m.group(2)
    if unit == "d":
        return datetime.now() + timedelta(days=amount)
    elif unit == "h":
        return datetime.now() + timedelta(hours=amount)
    elif unit == "m":
        return datetime.now() + timedelta(minutes=amount)
    return None


def _to_epoch(value) -> Optional[int]:
    """Best-effort coerce a value (int/float/ISO string) to a unix epoch second."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, datetime):
        return int(value.timestamp())
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        # Pure-numeric strings (already epoch)
        if s.lstrip("-").isdigit():
            try:
                return int(s)
            except ValueError:
                return None
        # ISO-format datetimes (legacy data)
        try:
            return int(datetime.fromisoformat(s).timestamp())
        except ValueError:
            return None
    return None


def _epoch_to_iso_date(epoch: Optional[int]) -> Optional[str]:
    """Format an epoch second as YYYY-MM-DD for human-readable display."""
    if epoch is None:
        return None
    try:
        return datetime.fromtimestamp(int(epoch)).strftime("%Y-%m-%d")
    except (ValueError, OSError, OverflowError):
        return None


def _normalize_iso_timestamp(value) -> str:
    """Coerce a timestamp value into an ISO-8601 string.

    ``import_all`` writes ``created_at`` and ``updated_at`` straight into the
    DB, where downstream code does ``ts[:10]`` to render the date prefix. If
    the snapshot smuggles in an ``int`` epoch or a ``datetime`` object (from a
    JSON export tool that round-trips dates differently, or a hand-edited
    backup), that string-slice would crash on the very first read.

    Strategy:
      - bare ``str`` that isn't empty → keep it (assume already ISO; if it
        isn't, downstream slicing returns garbage but doesn't crash)
      - ``datetime`` → ``.isoformat()``
      - ``int`` / ``float`` → treated as unix epoch
      - everything else → ``datetime.now().isoformat()`` as a safe default
    """
    if isinstance(value, str) and value:
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value)).isoformat()
        except (ValueError, OSError, OverflowError):
            return datetime.now().isoformat()
    return datetime.now().isoformat()


class UserMemoryDB:
    """Thread-safe SQLite wrapper for user memory persistence."""

    @staticmethod
    def _sanitize(text: str) -> str:
        """Sanitize text for safe embedding in structured context.

        Replaces XML-like angle brackets with fullwidth variants and
        collapses newlines to spaces so injected content cannot break
        out of <context>...</context> or similar delimiters.
        """
        return (
            str(text)
            .replace("<", "＜")
            .replace(">", "＞")
            .replace("\r", " ")
            .replace("\n", " ")
        )

    def __init__(self, db_path: str, *, enable_fts5: bool = True):
        self.db_path = db_path
        self._tls = threading.local()
        # Registry of every connection ever handed out (one per thread that
        # touched the DB), so close() can close all of them.
        self._conn_registry: List[sqlite3.Connection] = []
        self._registry_lock = Lock()
        self._write_lock = Lock()
        self._closed = False
        # FTS5 toggle — flipped to False inside _init_extended_schema if the
        # installed SQLite wasn't compiled with FTS5 support.
        self._enable_fts5 = bool(enable_fts5)
        self._ensure_dir()
        # Initialize schema (runs in the caller's thread; that connection is
        # registered like any other).
        self._init_db()

    def _ensure_dir(self):
        dir_path = os.path.dirname(self.db_path)
        if dir_path:
            os.makedirs(dir_path, exist_ok=True)

    def _get_conn(self) -> sqlite3.Connection:
        """Return this thread's connection, creating it on first access.

        The closed-check covers **both** the "cached tls connection" and the
        "create new connection" paths — under the same ``_registry_lock``.
        Without this, a thread that already had a cached connection would
        keep getting that handle back even after ``close()`` had closed it,
        producing cryptic SQLite "Cannot operate on a closed database" errors
        far from the real problem instead of a clean RuntimeError at the
        point the caller tried to use the DB.
        """
        with self._registry_lock:
            if self._closed:
                raise RuntimeError("UserMemoryDB has been closed")
            conn = getattr(self._tls, "conn", None)
            if conn is None:
                conn = sqlite3.connect(
                    self.db_path, check_same_thread=False, timeout=10.0
                )
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
                conn.execute("PRAGMA foreign_keys=ON")
                self._tls.conn = conn
                self._conn_registry.append(conn)
            return conn

    def close(self):
        """Close every per-thread connection that was opened."""
        with self._registry_lock:
            self._closed = True
            for conn in self._conn_registry:
                try:
                    conn.close()
                except Exception as e:
                    logger.warning(f"Error closing SQLite connection: {e}")
            self._conn_registry.clear()
        # Drop the local cache too (so a subsequent reopen on this thread won't
        # hand out a closed handle).
        try:
            del self._tls.conn
        except AttributeError:
            pass

    def _init_db(self):
        """Create tables if they don't exist, and migrate old schemas."""
        with self._write_lock:
            conn = self._get_conn()
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS user_profiles (
                    user_id      TEXT NOT NULL,
                    memory_key   TEXT NOT NULL,
                    memory_value TEXT NOT NULL,
                    updated_at   DATETIME DEFAULT CURRENT_TIMESTAMP,
                    confidence   REAL DEFAULT 0.5,
                    category     TEXT DEFAULT 'basic',
                    expires_at   INTEGER DEFAULT NULL,
                    PRIMARY KEY (user_id, memory_key)
                );

                CREATE TABLE IF NOT EXISTS event_logs (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id       TEXT NOT NULL,
                    event_summary TEXT NOT NULL,
                    created_at    DATETIME DEFAULT CURRENT_TIMESTAMP,
                    tag           TEXT DEFAULT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_event_logs_user
                    ON event_logs (user_id, created_at DESC);
            """)
            conn.commit()
            self._migrate(conn)
            self._init_extended_schema(conn)
            logger.info(f"User memory database initialized at {self.db_path}")

    def _migrate(self, conn: sqlite3.Connection):
        """Auto-add new columns and rewrite legacy ISO-string expires_at values.

        Each new column appended here will be NULL for pre-existing rows;
        no reader code is permitted to fail when these are NULL.
        """
        cursor = conn.execute("PRAGMA table_info(user_profiles)")
        existing_cols = {row[1] for row in cursor.fetchall()}
        migrations = [
            ("confidence", "REAL DEFAULT 0.5"),
            ("category", "TEXT DEFAULT 'basic'"),
            ("expires_at", "INTEGER DEFAULT NULL"),
            # Phase 1 — NEKO-inspired persona / evidence / embedding columns.
            # entity + relation_type let a profile row participate in the
            # entity graph (e.g. entity='neko', relation_type='likes_food');
            # source records who wrote the row ('llm' | 'auditor' |
            # 'reflection_promote' | 'user_directive'); protected=1 pins a
            # row against eviction regardless of evidence_score (see
            # cognition/evidence.py); embedding holds an fp16 base64-encoded
            # vector blob and embedding_sha caches the model+text hash so we
            # can cheaply detect "vector is still valid for this value".
            ("entity", "TEXT DEFAULT NULL"),
            ("relation_type", "TEXT DEFAULT NULL"),
            ("source", "TEXT DEFAULT 'llm'"),
            ("protected", "INTEGER DEFAULT 0"),
            ("embedding", "BLOB DEFAULT NULL"),
            ("embedding_sha", "TEXT DEFAULT NULL"),
        ]
        for col_name, col_def in migrations:
            if col_name not in existing_cols:
                conn.execute(f"ALTER TABLE user_profiles ADD COLUMN {col_name} {col_def}")
                logger.info(f"Migrated user_profiles: added column '{col_name}'")
        conn.commit()

        cursor = conn.execute("PRAGMA table_info(event_logs)")
        event_cols = {row[1] for row in cursor.fetchall()}
        event_migrations = [
            ("tag", "TEXT DEFAULT NULL"),
            # Phase 1 — fact identity (fact_hash for dedup), importance for
            # rein-seed mapping (cognition/evidence.py:initial_reinforcement
            # _from_importance), absorbed=1 once a reflection has consumed
            # this fact, and an optional vector for semantic recall.
            ("fact_hash", "TEXT DEFAULT NULL"),
            ("importance", "INTEGER DEFAULT 5"),
            ("absorbed", "INTEGER DEFAULT 0"),
            ("embedding", "BLOB DEFAULT NULL"),
            ("embedding_sha", "TEXT DEFAULT NULL"),
        ]
        for col_name, col_def in event_migrations:
            if col_name not in event_cols:
                conn.execute(f"ALTER TABLE event_logs ADD COLUMN {col_name} {col_def}")
                logger.info(f"Migrated event_logs: added column '{col_name}'")
        conn.commit()

        # One-shot conversion: any expires_at still stored as TEXT (legacy ISO)
        # gets rewritten to INTEGER epoch so subsequent reads can use a plain
        # numeric comparison.
        cur = conn.execute(
            "SELECT rowid, expires_at FROM user_profiles "
            "WHERE expires_at IS NOT NULL AND typeof(expires_at) = 'text'"
        )
        rows = cur.fetchall()
        if rows:
            converted = 0
            for rowid, raw in rows:
                epoch = _to_epoch(raw)
                conn.execute(
                    "UPDATE user_profiles SET expires_at = ? WHERE rowid = ?",
                    (epoch, rowid),
                )
                converted += 1
            conn.commit()
            logger.info(f"Migrated {converted} expires_at value(s) from ISO text to epoch integer")

    # ── Phase 1: Extended schema (NEKO-inspired cognition tables) ───

    def _init_extended_schema(self, conn: sqlite3.Connection):
        """Create supplementary tables + indexes + optional FTS5 index.

        Runs after ``_migrate`` so the new columns on base tables already
        exist. All statements are idempotent — running this on a fresh DB,
        a partially migrated DB, or a fully up-to-date DB all reach the
        same state.

        The reflections + evidence_ledger tables are wired in Phase 3;
        Phase 1 only provisions them so subsequent migrations don't have
        to redo schema work.

        Caller contract: ``_write_lock`` is already held (invoked from
        ``_init_db`` inside the same ``with`` block). ``threading.Lock``
        is non-reentrant, so re-acquiring would deadlock.
        """
        conn.executescript(
            """
            CREATE INDEX IF NOT EXISTS idx_event_logs_fact_hash
                ON event_logs (user_id, fact_hash);
            CREATE INDEX IF NOT EXISTS idx_event_logs_absorbed
                ON event_logs (user_id, absorbed, created_at);
            CREATE INDEX IF NOT EXISTS idx_user_profiles_entity
                ON user_profiles (user_id, entity);

            -- Tier-2 reflections: multi-fact syntheses that live in
            -- a finite-state machine (see cognition/reflection.py).
            CREATE TABLE IF NOT EXISTS reflections (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id         TEXT NOT NULL,
                summary         TEXT NOT NULL,
                entity          TEXT,
                relation_type   TEXT,
                source_fact_ids TEXT,
                status          TEXT DEFAULT 'pending',
                created_at      INTEGER NOT NULL,
                updated_at      INTEGER NOT NULL,
                promoted_at     INTEGER,
                embedding       BLOB,
                embedding_sha   TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_reflections_user_status
                ON reflections (user_id, status);
            CREATE INDEX IF NOT EXISTS idx_reflections_status_age
                ON reflections (status, created_at);

            -- Evidence ledger: rein/disp with independent half-life
            -- clocks (RFC §3.1.1, evidence.py). Shared by profile +
            -- reflection rows via the (target_kind, target_id) tuple.
            CREATE TABLE IF NOT EXISTS evidence_ledger (
                id                          INTEGER PRIMARY KEY AUTOINCREMENT,
                target_kind                 TEXT NOT NULL,
                target_id                   TEXT NOT NULL,
                rein                        REAL DEFAULT 0,
                disp                        REAL DEFAULT 0,
                rein_last_signal_at         INTEGER,
                disp_last_signal_at         INTEGER,
                sub_zero_days               REAL DEFAULT 0,
                user_fact_reinforce_count   INTEGER DEFAULT 0,
                UNIQUE (target_kind, target_id)
            );
            CREATE INDEX IF NOT EXISTS idx_evidence_target
                ON evidence_ledger (target_kind, target_id);
            """
        )
        conn.commit()

        if self._enable_fts5:
            try:
                self._init_fts5(conn)
            except sqlite3.OperationalError as e:
                # FTS5 isn't compiled into this SQLite build (rare on
                # modern Python distributions, common on Alpine/min
                # builds). Degrade gracefully to LIKE-based search.
                logger.warning(
                    f"FTS5 unavailable ({e}); falling back to LIKE search. "
                    "Set enable_fts5=false in plugin config to silence."
                )
                self._enable_fts5 = False
        else:
            # User-disabled — tear down anything an earlier run created
            # so we don't keep half-stale triggers updating a phantom
            # table that the rest of the code no longer queries.
            self._drop_fts5(conn)

    def _init_fts5(self, conn: sqlite3.Connection):
        """Create FTS5 virtual tables + sync triggers (idempotent).

        ``event_logs_fts`` uses external-content mode (content='event_logs'),
        so no data duplication — the FTS index just stores tokens that
        reference rowid back into event_logs. Three triggers keep it in
        sync on INSERT / DELETE / UPDATE.

        On first creation against an existing DB, we issue the FTS5
        'rebuild' command so historical rows become searchable
        immediately rather than only future writes. We detect that
        first-creation case by probing sqlite_master BEFORE the CREATE
        statement — an external-content FTS5 table's ``COUNT(*)`` always
        reports the base-table count regardless of index state, so the
        naive "is FTS empty?" check would never fire.
        """
        fts_existed = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name = 'event_logs_fts'"
        ).fetchone() is not None
        # FTS5 external-content tables require column names to match the
        # content table exactly — the indexer reads them back via
        # SELECT <col> FROM <content_table>, so a mismatch surfaces as
        # "no such column: T.summary" at rebuild time. We mirror
        # event_logs' columns 1:1 here. Callers that want column-agnostic
        # search can use `event_logs_fts MATCH 'kw'`; for column-scoped
        # search use `event_summary MATCH 'kw'`.
        #
        # Tokenizer choice — `trigram` (SQLite 3.34+):
        #   unicode61 stores each contiguous Han run as ONE token, so a
        #   query like MATCH '跑步' would never hit "今天去跑步了" (a
        #   single fused token). trigram extracts overlapping 3-char
        #   windows, which lets CJK phrases of ≥ 3 chars search correctly
        #   and still indexes English words for substring matches.
        #   Queries of 1-2 chars (CJK or otherwise) return no FTS hits;
        #   callers should fall back to LIKE for those (handled in
        #   recall.py in Phase 2).
        conn.executescript(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS event_logs_fts USING fts5(
                user_id UNINDEXED,
                event_summary,
                tag UNINDEXED,
                content='event_logs',
                content_rowid='id',
                tokenize='trigram'
            );
            CREATE TRIGGER IF NOT EXISTS event_logs_ai
                AFTER INSERT ON event_logs BEGIN
                    INSERT INTO event_logs_fts(rowid, user_id, event_summary, tag)
                    VALUES (new.id, new.user_id, new.event_summary,
                            COALESCE(new.tag, ''));
                END;
            CREATE TRIGGER IF NOT EXISTS event_logs_ad
                AFTER DELETE ON event_logs BEGIN
                    INSERT INTO event_logs_fts(event_logs_fts, rowid, user_id, event_summary, tag)
                    VALUES ('delete', old.id, old.user_id, old.event_summary,
                            COALESCE(old.tag, ''));
                END;
            CREATE TRIGGER IF NOT EXISTS event_logs_au
                AFTER UPDATE ON event_logs BEGIN
                    INSERT INTO event_logs_fts(event_logs_fts, rowid, user_id, event_summary, tag)
                    VALUES ('delete', old.id, old.user_id, old.event_summary,
                            COALESCE(old.tag, ''));
                    INSERT INTO event_logs_fts(rowid, user_id, event_summary, tag)
                    VALUES (new.id, new.user_id, new.event_summary,
                            COALESCE(new.tag, ''));
                END;
            """
        )
        conn.commit()
        # Backfill historical rows on first-time creation only. 'rebuild'
        # is O(N) over event_logs and would be wasteful on every startup
        # for a large DB. The fts_existed probe above tells us whether
        # this is a brand-new index or an existing one that the triggers
        # have been keeping in sync.
        if not fts_existed:
            base_count = conn.execute(
                "SELECT COUNT(*) FROM event_logs"
            ).fetchone()[0]
            if base_count > 0:
                conn.execute(
                    "INSERT INTO event_logs_fts(event_logs_fts) VALUES ('rebuild')"
                )
                conn.commit()
                logger.info(
                    f"FTS5: rebuilt index from {base_count} existing event_logs rows"
                )

    def _drop_fts5(self, conn: sqlite3.Connection):
        """Tear down FTS objects when the user disables FTS via config."""
        conn.executescript(
            """
            DROP TRIGGER IF EXISTS event_logs_ai;
            DROP TRIGGER IF EXISTS event_logs_ad;
            DROP TRIGGER IF EXISTS event_logs_au;
            DROP TABLE IF EXISTS event_logs_fts;
            """
        )
        conn.commit()

    @property
    def fts5_enabled(self) -> bool:
        """Whether FTS5 is currently active for this DB instance.

        May differ from the constructor argument if FTS5 wasn't compiled
        into the running SQLite — _init_fts5 catches that and flips the
        flag to False so callers can fall back to LIKE search.
        """
        return self._enable_fts5

    # ── Profile Operations ──────────────────────────────────────────

    @staticmethod
    def _clip_value(value: str, limit: int) -> Tuple[str, bool]:
        """Truncate *value* to *limit* characters. Returns (clipped, was_truncated)."""
        s = "" if value is None else str(value)
        if len(s) <= limit:
            return s, False
        # Reserve a few chars for the ellipsis marker so total length stays within limit
        return s[: max(0, limit - 1)] + "…", True

    def save_profile(self, user_id: str, key: str, value: str, *,
                     confidence: float = 0.5, category: str = "basic",
                     expires_at=None) -> None:
        """Insert or update a user profile entry with metadata.

        *expires_at* may be a unix epoch int, an ISO-format string, a datetime,
        or None. It is normalized to integer epoch seconds for storage.
        Long values are truncated to ``MAX_PROFILE_VALUE_LEN``.
        """
        if category not in VALID_CATEGORIES:
            category = "other"
        confidence = max(0.0, min(1.0, confidence))
        expires_epoch = _to_epoch(expires_at)
        clipped, was_truncated = self._clip_value(value, MAX_PROFILE_VALUE_LEN)
        if was_truncated:
            logger.warning(
                f"Profile value for {_mask_id(user_id)}/{_mask_id(key)} truncated "
                f"from {len(str(value))} to {MAX_PROFILE_VALUE_LEN} chars"
            )
        with self._write_lock:
            conn = self._get_conn()
            conn.execute(
                """INSERT INTO user_profiles
                       (user_id, memory_key, memory_value, updated_at, confidence, category, expires_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(user_id, memory_key)
                   DO UPDATE SET memory_value = excluded.memory_value,
                                 updated_at   = excluded.updated_at,
                                 confidence   = excluded.confidence,
                                 category     = excluded.category,
                                 expires_at   = excluded.expires_at""",
                (user_id, key, clipped, datetime.now().isoformat(),
                 confidence, category, expires_epoch)
            )
            conn.commit()

    def upsert_with_limit(self, user_id: str, key: str, value: str, *,
                          max_profiles: int,
                          confidence: float = 0.5, category: str = "basic",
                          expires_at=None,
                          force: bool = False) -> Tuple[str, dict]:
        """Atomically check existence/limit, optional pre-flight conflict check, and upsert.

        Returns ``(status, info)`` where ``status`` is one of:

          - ``"set"``             — new key inserted
          - ``"updated"``         — existing key overwritten (no conflict)
          - ``"truncated"``       — written, but value was truncated; ``info["truncated_from"]`` holds original length
          - ``"limit_exceeded"``  — would exceed *max_profiles*, rejected. ``info["count"]`` is the existing count.
          - ``"conflict"``        — would overwrite a higher-confidence value with a different value;
                                    ``info`` contains ``existing_value``, ``existing_confidence``,
                                    ``new_confidence`` and a ``hint`` string.
                                    Pass ``force=True`` (or supply a confidence within
                                    ``CONFLICT_CONFIDENCE_GAP`` of the existing one) to override.

        ``info`` is always a dict so the caller can pattern-match safely.
        """
        if category not in VALID_CATEGORIES:
            category = "other"
        confidence = max(0.0, min(1.0, confidence))
        expires_epoch = _to_epoch(expires_at)
        now_epoch = int(time.time())
        clipped, was_truncated = self._clip_value(value, MAX_PROFILE_VALUE_LEN)
        info: dict = {}

        with self._write_lock:
            conn = self._get_conn()
            try:
                conn.execute("BEGIN IMMEDIATE")
                # Fetch existing row (non-expired) so we can do conflict detection
                cur = conn.execute(
                    "SELECT memory_value, confidence FROM user_profiles "
                    "WHERE user_id = ? AND memory_key = ? "
                    "AND (expires_at IS NULL OR expires_at > ?) LIMIT 1",
                    (user_id, key, now_epoch),
                )
                row = cur.fetchone()
                is_update = row is not None

                if not is_update:
                    cur = conn.execute(
                        "SELECT COUNT(*) FROM user_profiles "
                        "WHERE user_id = ? AND (expires_at IS NULL OR expires_at > ?)",
                        (user_id, now_epoch),
                    )
                    count = cur.fetchone()[0]
                    if count >= max_profiles:
                        conn.rollback()
                        return ("limit_exceeded", {"count": count})

                # Pre-flight conflict check: only reject when the existing entry
                # is meaningfully more confident AND the value is actually changing.
                if is_update and not force:
                    existing_value, existing_conf = row
                    existing_conf = existing_conf if existing_conf is not None else 0.5
                    if (existing_value != clipped
                            and existing_conf - confidence > CONFLICT_CONFIDENCE_GAP):
                        conn.rollback()
                        return ("conflict", {
                            "existing_value": existing_value,
                            "existing_confidence": existing_conf,
                            "new_value": clipped,
                            "new_confidence": confidence,
                            "hint": (
                                f"现值 '{existing_value}' (置信度 {existing_conf:.2f}) "
                                f"高于新值置信度 {confidence:.2f}。如确认要覆盖，"
                                f"请提高 confidence 至 ≥{existing_conf - CONFLICT_CONFIDENCE_GAP:.2f} "
                                f"或在操作上加 force=true。"
                            ),
                        })

                conn.execute(
                    """INSERT INTO user_profiles
                           (user_id, memory_key, memory_value, updated_at, confidence, category, expires_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(user_id, memory_key)
                       DO UPDATE SET memory_value = excluded.memory_value,
                                     updated_at   = excluded.updated_at,
                                     confidence   = excluded.confidence,
                                     category     = excluded.category,
                                     expires_at   = excluded.expires_at""",
                    (user_id, key, clipped, datetime.now().isoformat(),
                     confidence, category, expires_epoch),
                )
                conn.commit()

                if was_truncated:
                    info["truncated_from"] = len(str(value))
                    return ("truncated", info)
                return ("updated" if is_update else "set", info)
            except Exception:
                try:
                    conn.rollback()
                except Exception as rb_err:
                    # Don't swallow silently — a failed rollback can leave a
                    # dirty transaction that breaks the next write. Logging
                    # surfaces it so we can correlate with the outer error.
                    logger.warning(
                        f"upsert_with_limit: rollback failed for "
                        f"{_mask_id(user_id)}/{_mask_id(key)}: {rb_err}"
                    )
                raise

    def get_profiles(self, user_id: str, *, include_expired: bool = False,
                     category: Optional[str] = None
                     ) -> List[Tuple[str, str, str, float, str, Optional[int]]]:
        """Return profile entries as (key, value, updated_at, confidence, category, expires_at)."""
        clauses = ["user_id = ?"]
        params: list = [user_id]
        if not include_expired:
            clauses.append("(expires_at IS NULL OR expires_at > ?)")
            params.append(int(time.time()))
        if category is not None:
            clauses.append("category = ?")
            params.append(category)
        sql = (
            "SELECT memory_key, memory_value, updated_at, confidence, category, expires_at "
            "FROM user_profiles WHERE " + " AND ".join(clauses) +
            " ORDER BY datetime(updated_at) DESC"
        )
        conn = self._get_conn()
        return conn.execute(sql, params).fetchall()

    def get_profiles_by_category(self, user_id: str, category: str
                                 ) -> List[Tuple[str, str, float]]:
        """Return (key, value, confidence) for a specific category, excluding expired."""
        conn = self._get_conn()
        cursor = conn.execute(
            """SELECT memory_key, memory_value, confidence FROM user_profiles
               WHERE user_id = ? AND category = ?
                 AND (expires_at IS NULL OR expires_at > ?)
               ORDER BY confidence DESC, datetime(updated_at) DESC""",
            (user_id, category, int(time.time()))
        )
        return cursor.fetchall()

    def remove_profile(self, user_id: str, key: str) -> bool:
        """Remove a specific profile entry. Returns True if a row was deleted."""
        with self._write_lock:
            conn = self._get_conn()
            cursor = conn.execute(
                "DELETE FROM user_profiles WHERE user_id = ? AND memory_key = ?",
                (user_id, key)
            )
            conn.commit()
            return cursor.rowcount > 0

    def profile_exists(self, user_id: str, key: str) -> bool:
        """Return True if a non-expired profile entry exists."""
        conn = self._get_conn()
        cursor = conn.execute(
            "SELECT 1 FROM user_profiles "
            "WHERE user_id = ? AND memory_key = ? "
            "AND (expires_at IS NULL OR expires_at > ?) LIMIT 1",
            (user_id, key, int(time.time()))
        )
        return cursor.fetchone() is not None

    def get_profile_count(self, user_id: str) -> int:
        """Return the number of non-expired profile entries for a user."""
        conn = self._get_conn()
        cursor = conn.execute(
            "SELECT COUNT(*) FROM user_profiles "
            "WHERE user_id = ? AND (expires_at IS NULL OR expires_at > ?)",
            (user_id, int(time.time()))
        )
        return cursor.fetchone()[0]

    def clear_user_memory(self, user_id: str) -> Tuple[int, int]:
        """Delete all profiles and events for a user. Returns (profiles_deleted, events_deleted)."""
        with self._write_lock:
            conn = self._get_conn()
            c1 = conn.execute("DELETE FROM user_profiles WHERE user_id = ?", (user_id,))
            c2 = conn.execute("DELETE FROM event_logs WHERE user_id = ?", (user_id,))
            conn.commit()
            return c1.rowcount, c2.rowcount

    # ── Statistics (for WebUI) ─────────────────────────────────────

    def get_stats(self) -> Dict:
        """Return global statistics: total users, profiles, and events."""
        conn = self._get_conn()
        total_users = conn.execute(
            """SELECT COUNT(*) FROM (
                   SELECT user_id FROM user_profiles
                   UNION
                   SELECT user_id FROM event_logs
               )"""
        ).fetchone()[0]
        total_profiles = conn.execute("SELECT COUNT(*) FROM user_profiles").fetchone()[0]
        total_events = conn.execute("SELECT COUNT(*) FROM event_logs").fetchone()[0]
        return {
            "total_users": total_users,
            "total_profiles": total_profiles,
            "total_events": total_events,
        }

    # ── User Listing (for WebUI) ───────────────────────────────────

    def list_users(self) -> List[Dict]:
        """Return a summary of all users with profile and event counts."""
        conn = self._get_conn()
        cursor = conn.execute(
            """SELECT uid,
                      COALESCE(pc, 0) AS profile_count,
                      COALESCE(ec, 0) AS event_count
               FROM (
                   SELECT user_id AS uid FROM user_profiles
                   UNION
                   SELECT user_id AS uid FROM event_logs
               ) all_users
               LEFT JOIN (
                   SELECT user_id, COUNT(*) AS pc FROM user_profiles GROUP BY user_id
               ) p ON p.user_id = uid
               LEFT JOIN (
                   SELECT user_id, COUNT(*) AS ec FROM event_logs GROUP BY user_id
               ) e ON e.user_id = uid
               ORDER BY uid"""
        )
        return [
            {"user_id": row[0], "profile_count": row[1], "event_count": row[2]}
            for row in cursor.fetchall()
        ]

    def delete_event(self, event_id: int, user_id: str | None = None) -> bool:
        """Delete a single event log entry by its ID. If user_id is given, also verify ownership."""
        with self._write_lock:
            conn = self._get_conn()
            if user_id is not None:
                cursor = conn.execute(
                    "DELETE FROM event_logs WHERE id = ? AND user_id = ?",
                    (event_id, user_id),
                )
            else:
                cursor = conn.execute(
                    "DELETE FROM event_logs WHERE id = ?", (event_id,)
                )
            conn.commit()
            return cursor.rowcount > 0

    def get_events_with_id(self, user_id: str, limit: int = 100
                           ) -> List[Tuple[int, str, str, Optional[str]]]:
        """Return recent events with IDs as (id, event_summary, created_at, tag) tuples."""
        limit = max(limit, 0)
        conn = self._get_conn()
        cursor = conn.execute(
            "SELECT id, event_summary, created_at, tag FROM event_logs "
            "WHERE user_id = ? ORDER BY datetime(created_at) DESC LIMIT ?",
            (user_id, limit)
        )
        return cursor.fetchall()

    # ── Event Log Operations ────────────────────────────────────────

    def save_event(self, user_id: str, event_summary: str,
                   tag: Optional[str] = None) -> int:
        """Append an event log entry. Returns the new row id.

        Long summaries are truncated to ``MAX_EVENT_LEN``.
        """
        clipped, was_truncated = self._clip_value(event_summary, MAX_EVENT_LEN)
        if was_truncated:
            logger.warning(
                f"Event summary for {_mask_id(user_id)} truncated "
                f"from {len(str(event_summary))} to {MAX_EVENT_LEN} chars"
            )
        tag_norm = tag.strip() if isinstance(tag, str) and tag.strip() else None
        with self._write_lock:
            conn = self._get_conn()
            cur = conn.execute(
                "INSERT INTO event_logs (user_id, event_summary, created_at, tag) VALUES (?, ?, ?, ?)",
                (user_id, clipped, datetime.now().isoformat(), tag_norm)
            )
            conn.commit()
            return cur.lastrowid

    def update_event(self, event_id: int, event_summary: str, user_id: str | None = None,
                     tag: Optional[str] = None, set_tag: bool = False) -> bool:
        """Update an existing event's summary (and optionally its tag).

        If *set_tag* is True the tag is overwritten (pass tag=None to clear).
        If *user_id* is given, ownership is also verified.
        """
        clipped, was_truncated = self._clip_value(event_summary, MAX_EVENT_LEN)
        if was_truncated:
            logger.warning(
                f"Event update truncated from {len(str(event_summary))} "
                f"to {MAX_EVENT_LEN} chars (id={event_id})"
            )
        tag_norm = (tag.strip() if isinstance(tag, str) and tag.strip() else None) if set_tag else None
        with self._write_lock:
            conn = self._get_conn()
            if set_tag:
                if user_id is not None:
                    cursor = conn.execute(
                        "UPDATE event_logs SET event_summary = ?, tag = ? WHERE id = ? AND user_id = ?",
                        (clipped, tag_norm, event_id, user_id),
                    )
                else:
                    cursor = conn.execute(
                        "UPDATE event_logs SET event_summary = ?, tag = ? WHERE id = ?",
                        (clipped, tag_norm, event_id),
                    )
            else:
                if user_id is not None:
                    cursor = conn.execute(
                        "UPDATE event_logs SET event_summary = ? WHERE id = ? AND user_id = ?",
                        (clipped, event_id, user_id),
                    )
                else:
                    cursor = conn.execute(
                        "UPDATE event_logs SET event_summary = ? WHERE id = ?",
                        (clipped, event_id),
                    )
            conn.commit()
            return cursor.rowcount > 0

    def get_recent_events(self, user_id: str, limit: int = 5,
                          tags: Optional[List[str]] = None
                          ) -> List[Tuple[str, str, Optional[str]]]:
        """Return the most recent events for a user.

        Returns ``(event_summary, created_at, tag)`` tuples. If *tags* is given,
        only events whose ``tag`` is in that list are returned (untagged events
        are included only if ``None`` is in the list explicitly).
        """
        limit = max(limit, 0)
        conn = self._get_conn()
        if tags is None:
            cursor = conn.execute(
                "SELECT event_summary, created_at, tag FROM event_logs WHERE user_id = ? "
                "ORDER BY datetime(created_at) DESC LIMIT ?",
                (user_id, limit)
            )
        else:
            include_null = None in tags
            real_tags = [t for t in tags if t is not None]
            placeholders = ",".join("?" * len(real_tags))
            clauses: list = []
            params: list = [user_id]
            if real_tags:
                clauses.append(f"tag IN ({placeholders})")
                params.extend(real_tags)
            if include_null:
                clauses.append("tag IS NULL")
            tag_clause = " OR ".join(clauses) if clauses else "0"  # vacuous when no tags listed
            cursor = conn.execute(
                f"SELECT event_summary, created_at, tag FROM event_logs "
                f"WHERE user_id = ? AND ({tag_clause}) "
                f"ORDER BY datetime(created_at) DESC LIMIT ?",
                [*params, limit]
            )
        return cursor.fetchall()

    def get_event_count(self, user_id: str) -> int:
        """Return the number of event logs for a user."""
        conn = self._get_conn()
        cursor = conn.execute(
            "SELECT COUNT(*) FROM event_logs WHERE user_id = ?",
            (user_id,)
        )
        return cursor.fetchone()[0]

    # ── Phase 2: query-aware event recall ───────────────────────────

    # Below this length we skip FTS5 entirely and go straight to LIKE.
    # trigram-tokenized FTS5 returns nothing for sub-3-char MATCH queries
    # (each token is exactly 3 chars), so a short CJK query like "猫"
    # would otherwise produce an empty FTS result and a confusing "no
    # match" downstream — even though the user clearly meant a substring
    # lookup. LIKE picks up these cases cleanly.
    FTS_MIN_QUERY_LEN = 3

    def search_events_fts(
        self,
        user_id: str,
        query: str,
        limit: int = 20,
    ) -> List[Tuple[int, str, str, Optional[str], float]]:
        """BM25-ranked search via the FTS5 index.

        Returns ``[(id, event_summary, created_at, tag, bm25_score), …]``
        ordered best-first. SQLite's ``bm25()`` is signed: more-negative =
        more-relevant, so we negate for an intuitive "higher is better"
        score in the result tuple.

        Falls back to ``search_events_like`` when FTS5 is disabled (or
        unavailable at runtime) or when the query is below the trigram
        tokenizer's minimum length. Callers can rely on this always
        returning *something* for a non-empty query and corpus.

        The query is wrapped in an FTS5 phrase ("…") so callers don't have
        to escape MATCH operators themselves; an empty query short-circuits
        to an empty list rather than raising.
        """
        q = (query or "").strip()
        if not q:
            return []
        if not self._enable_fts5 or len(q) < self.FTS_MIN_QUERY_LEN:
            return self.search_events_like(user_id, q, limit=limit)
        # Escape embedded double quotes so the FTS5 MATCH phrase stays
        # well-formed. The pattern ""…"" is the FTS5-native escape for a
        # literal " inside a phrase. Without this, a query like 谁说"对"
        # would be parsed as two separate phrases.
        phrase = '"' + q.replace('"', '""') + '"'
        conn = self._get_conn()
        try:
            cursor = conn.execute(
                "SELECT e.id, e.event_summary, e.created_at, e.tag, "
                "       bm25(event_logs_fts) AS score "
                "FROM event_logs e "
                "JOIN event_logs_fts f ON f.rowid = e.id "
                "WHERE f.event_logs_fts MATCH ? AND e.user_id = ? "
                "ORDER BY score LIMIT ?",
                (phrase, user_id, max(int(limit), 0)),
            )
        except sqlite3.OperationalError as exc:
            # Malformed FTS5 expression (rare — phrase quoting should
            # prevent it) or runtime FTS5 unavailability. Log once and
            # serve a LIKE-based fallback so the caller still gets data.
            logger.warning(
                f"FTS5 query failed ({exc}); falling back to LIKE for "
                f"user={_mask_id(user_id)} query={q!r}"
            )
            return self.search_events_like(user_id, q, limit=limit)
        rows = cursor.fetchall()
        return [
            (row[0], row[1], row[2], row[3], -float(row[4]))
            for row in rows
        ]

    def search_events_like(
        self,
        user_id: str,
        query: str,
        limit: int = 20,
    ) -> List[Tuple[int, str, str, Optional[str], float]]:
        """Substring search via SQL LIKE. The score in the returned tuple
        is a synthetic ``1.0`` so the shape matches ``search_events_fts``
        and downstream re-ranking can blend the two sources.

        Used as the universal fallback when FTS5 isn't an option (config
        off, runtime failure, sub-trigram-length query). The ESCAPE clause
        neutralizes literal %, _, and \\ in the user's query so a search
        for ``100%`` doesn't wildcard-match the entire corpus.
        """
        q = (query or "").strip()
        if not q:
            return []
        # SQLite's LIKE wildcards are % and _; backslash is our chosen
        # escape character. Escape it FIRST so we don't re-escape the
        # escape introductions added for % / _.
        escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped}%"
        conn = self._get_conn()
        cursor = conn.execute(
            "SELECT id, event_summary, created_at, tag "
            "FROM event_logs WHERE user_id = ? "
            "AND event_summary LIKE ? ESCAPE '\\' "
            "ORDER BY datetime(created_at) DESC LIMIT ?",
            (user_id, pattern, max(int(limit), 0)),
        )
        return [
            (row[0], row[1], row[2], row[3], 1.0)
            for row in cursor.fetchall()
        ]

    def search_profiles_like(
        self,
        user_id: str,
        query: str,
        limit: int = 20,
    ) -> List[Tuple[str, str, str, float, str]]:
        """Substring search over profile keys + values via SQL LIKE.

        Returns ``[(memory_key, memory_value, updated_at, confidence,
        category), …]``. Profiles are far fewer than events in typical
        use (capped by ``max_profiles_per_user``, default 50), so the
        LIKE-only path is fast enough — no FTS index for profiles yet.
        """
        q = (query or "").strip()
        if not q:
            return []
        escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped}%"
        conn = self._get_conn()
        now_epoch = int(time.time())
        cursor = conn.execute(
            "SELECT memory_key, memory_value, updated_at, confidence, category "
            "FROM user_profiles WHERE user_id = ? "
            "AND (expires_at IS NULL OR expires_at > ?) "
            "AND (memory_key LIKE ? ESCAPE '\\' OR memory_value LIKE ? ESCAPE '\\') "
            "ORDER BY confidence DESC, updated_at DESC LIMIT ?",
            (user_id, now_epoch, pattern, pattern, max(int(limit), 0)),
        )
        return cursor.fetchall()

    # ── Phase 3a: fact identity + evidence ledger ───────────────────

    @staticmethod
    def evidence_target_id_for_profile(user_id: str, memory_key: str) -> str:
        """Compose the (target_kind='profile', target_id=…) shape used
        in evidence_ledger so callers stay decoupled from the storage
        encoding. We embed user_id so cross-user profiles with the same
        key (e.g. two users both have 'nickname') don't share an
        evidence row.
        """
        return f"{user_id}::{memory_key}"

    def find_event_by_fact_hash(
        self,
        user_id: str,
        fact_hash: str,
    ) -> Optional[Tuple[int, str, int, Optional[str]]]:
        """Look up an existing event row by its content hash.

        Returns ``(id, event_summary, importance, tag)`` or None. Used
        by ``save_event_with_dedup`` and the auditor's Stage-1 fact
        extraction to decide whether a new fact is genuinely new or
        just a restatement of a previously-logged one.

        The (user_id, fact_hash) composite index added in Phase 1 makes
        this O(log N) regardless of corpus size.
        """
        if not fact_hash:
            return None
        conn = self._get_conn()
        row = conn.execute(
            "SELECT id, event_summary, importance, tag "
            "FROM event_logs "
            "WHERE user_id = ? AND fact_hash = ? "
            "ORDER BY id DESC LIMIT 1",
            (user_id, fact_hash),
        ).fetchone()
        return row if row else None

    def save_event_with_dedup(
        self,
        user_id: str,
        event_summary: str,
        *,
        fact_hash: str,
        importance: int = 5,
        tag: Optional[str] = None,
    ) -> Tuple[int, str]:
        """Insert an event or, if its fact_hash already exists for this
        user, return the existing row id (no insert).

        Returns ``(event_id, status)`` where status ∈ {"inserted", "deduped"}.
        Callers typically follow up with ``record_evidence_signal`` to
        attribute a rein impulse — Phase 3a memory_update wires this
        through automatically.

        Importance is updated to ``max(existing, new)`` on dedup so a
        later high-importance restatement doesn't get its signal
        downgraded by an earlier low-importance log of the same fact.
        """
        if not fact_hash:
            # No identity → can't dedup. Fall through to the original
            # save_event path. Callers should have screened by
            # is_dedup_candidate before invoking this.
            return self.save_event(user_id, event_summary, tag=tag), "inserted"

        existing = self.find_event_by_fact_hash(user_id, fact_hash)
        if existing is not None:
            eid, _, existing_imp, _ = existing
            new_imp = max(int(existing_imp or 5), int(importance))
            with self._write_lock:
                self._get_conn().execute(
                    "UPDATE event_logs SET importance = ? WHERE id = ?",
                    (new_imp, eid),
                )
                self._get_conn().commit()
            return eid, "deduped"

        # Genuinely new fact. We bypass save_event() here so we can pin
        # fact_hash + importance atomically with the insert and not race
        # a second writer that might try to insert the same hash.
        clipped, was_truncated = self._clip_value(event_summary, MAX_EVENT_LEN)
        if was_truncated:
            logger.warning(
                f"Event summary for {_mask_id(user_id)} truncated from "
                f"{len(str(event_summary))} to {MAX_EVENT_LEN} chars"
            )
        tag_norm = tag.strip() if isinstance(tag, str) and tag.strip() else None
        with self._write_lock:
            conn = self._get_conn()
            try:
                conn.execute("BEGIN IMMEDIATE")
                # Re-check inside the transaction so a parallel writer
                # that just inserted the same hash doesn't produce a
                # duplicate. UNIQUE(user_id, fact_hash) would be cleaner
                # but we can't add it post-migration without rebuilding
                # the table — the index covers the lookup anyway.
                race = conn.execute(
                    "SELECT id FROM event_logs "
                    "WHERE user_id = ? AND fact_hash = ? LIMIT 1",
                    (user_id, fact_hash),
                ).fetchone()
                if race is not None:
                    conn.commit()
                    return race[0], "deduped"
                cur = conn.execute(
                    "INSERT INTO event_logs "
                    "(user_id, event_summary, created_at, tag, "
                    " fact_hash, importance, absorbed) "
                    "VALUES (?, ?, ?, ?, ?, ?, 0)",
                    (user_id, clipped, datetime.now().isoformat(),
                     tag_norm, fact_hash, int(importance)),
                )
                conn.commit()
                return cur.lastrowid, "inserted"
            except Exception:
                try:
                    conn.rollback()
                except Exception:
                    pass
                raise

    def record_evidence_signal(
        self,
        target_kind: str,
        target_id: str,
        *,
        rein_delta: float = 0.0,
        disp_delta: float = 0.0,
        source: str = "auto",
        combo_threshold: int = 2,
        combo_bonus: float = 0.5,
    ) -> Dict:
        """Append a rein/disp impulse to the evidence ledger for a target.

        ``target_kind`` is ``'profile'`` or ``'reflection'``. ``target_id``
        is the matching key (see ``evidence_target_id_for_profile`` and,
        in Phase 3b, the reflection's primary key).

        The combo bonus mirrors ``cognition.evidence.compute_evidence_
        snapshot`` semantics — we replicate it here at the SQL layer so
        callers don't need to load the snapshot just to apply the
        increment. Pure functions on the snapshot still own the read-time
        decay; this method only manipulates the persisted counters.

        Returns the new (rein, disp, last_*_at, count) row as a dict for
        the caller's telemetry.
        """
        now_epoch = int(time.time())
        with self._write_lock:
            conn = self._get_conn()
            try:
                conn.execute("BEGIN IMMEDIATE")
                cur = conn.execute(
                    "SELECT rein, disp, rein_last_signal_at, "
                    "       disp_last_signal_at, sub_zero_days, "
                    "       user_fact_reinforce_count "
                    "FROM evidence_ledger "
                    "WHERE target_kind = ? AND target_id = ? LIMIT 1",
                    (target_kind, target_id),
                )
                row = cur.fetchone()
                if row is None:
                    rein = 0.0
                    disp = 0.0
                    rein_ts = None
                    disp_ts = None
                    sub_zero = 0.0
                    count = 0
                else:
                    rein, disp, rein_ts, disp_ts, sub_zero, count = row

                # Apply the delta. Disputation clamps non-negative; rein
                # can dip below zero on explicit negative writes (rare).
                rein = float(rein or 0.0) + float(rein_delta)
                disp = max(0.0, float(disp or 0.0) + float(disp_delta))
                # Only stamp the side that moved this turn — independent
                # half-life clocks (RFC §3.1.1).
                if rein_delta != 0.0:
                    rein_ts = now_epoch
                if disp_delta != 0.0:
                    disp_ts = now_epoch
                # Combo bonus for repeated user_fact reinforces.
                if source == "user_fact" and rein_delta > 0:
                    count = int(count or 0) + 1
                    if count > combo_threshold:
                        rein += float(combo_bonus)

                if row is None:
                    conn.execute(
                        "INSERT INTO evidence_ledger "
                        "(target_kind, target_id, rein, disp, "
                        " rein_last_signal_at, disp_last_signal_at, "
                        " sub_zero_days, user_fact_reinforce_count) "
                        "VALUES (?,?,?,?,?,?,?,?)",
                        (target_kind, target_id, rein, disp,
                         rein_ts, disp_ts, sub_zero, count),
                    )
                else:
                    conn.execute(
                        "UPDATE evidence_ledger SET "
                        "rein=?, disp=?, rein_last_signal_at=?, "
                        "disp_last_signal_at=?, "
                        "user_fact_reinforce_count=? "
                        "WHERE target_kind=? AND target_id=?",
                        (rein, disp, rein_ts, disp_ts, count,
                         target_kind, target_id),
                    )
                conn.commit()
                return {
                    "rein": rein, "disp": disp,
                    "rein_last_signal_at": rein_ts,
                    "disp_last_signal_at": disp_ts,
                    "user_fact_reinforce_count": count,
                    "source": source,
                }
            except Exception:
                try:
                    conn.rollback()
                except Exception:
                    pass
                raise

    def get_evidence_snapshot(
        self,
        target_kind: str,
        target_id: str,
    ) -> Optional[Dict]:
        """Read a ledger row as a dict, or None if untouched.

        Callers that want the effective decayed value should pass this
        to ``cognition.evidence.evidence_score`` along with their
        EvidenceConfig — the DB layer never computes decay itself, by
        design (see evidence.py's module docstring).
        """
        conn = self._get_conn()
        row = conn.execute(
            "SELECT rein, disp, rein_last_signal_at, disp_last_signal_at, "
            "       sub_zero_days, user_fact_reinforce_count "
            "FROM evidence_ledger "
            "WHERE target_kind = ? AND target_id = ? LIMIT 1",
            (target_kind, target_id),
        ).fetchone()
        if row is None:
            return None
        return {
            "rein": float(row[0] or 0.0),
            "disp": float(row[1] or 0.0),
            "rein_last_signal_at": row[2],
            "disp_last_signal_at": row[3],
            "sub_zero_days": float(row[4] or 0.0),
            "user_fact_reinforce_count": int(row[5] or 0),
        }

    # ── Phase 3a: reflection access (skeleton; populated in 3b) ─────

    def save_reflection(
        self,
        user_id: str,
        summary: str,
        *,
        entity: Optional[str] = None,
        relation_type: Optional[str] = None,
        source_fact_ids: Optional[List[int]] = None,
        status: str = "pending",
    ) -> int:
        """Insert a Tier-2 reflection. Phase 3a writers don't yet exist;
        Phase 3b's reflection synthesis stage is the primary caller.
        We provision the method now so the DB layer ships as one
        coherent surface and the sanity test can exercise it.
        """
        now = int(time.time())
        ids_json = json.dumps(source_fact_ids) if source_fact_ids else None
        with self._write_lock:
            conn = self._get_conn()
            cur = conn.execute(
                "INSERT INTO reflections "
                "(user_id, summary, entity, relation_type, source_fact_ids, "
                " status, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (user_id, summary, entity, relation_type, ids_json,
                 status, now, now),
            )
            conn.commit()
            return cur.lastrowid

    def list_reflections(
        self,
        user_id: str,
        *,
        status: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict]:
        """Return reflections for ``user_id`` filtered by status.

        Returns each row as a dict (not a tuple) so Phase 3b reconciler
        + Phase 5 WebUI can rely on stable field names regardless of
        future column additions.
        """
        conn = self._get_conn()
        if status is None:
            cursor = conn.execute(
                "SELECT id, summary, entity, relation_type, "
                "       source_fact_ids, status, created_at, "
                "       updated_at, promoted_at "
                "FROM reflections WHERE user_id = ? "
                "ORDER BY id DESC LIMIT ?",
                (user_id, max(int(limit), 0)),
            )
        else:
            cursor = conn.execute(
                "SELECT id, summary, entity, relation_type, "
                "       source_fact_ids, status, created_at, "
                "       updated_at, promoted_at "
                "FROM reflections WHERE user_id = ? AND status = ? "
                "ORDER BY id DESC LIMIT ?",
                (user_id, status, max(int(limit), 0)),
            )
        out = []
        for (rid, summary, entity, relation_type, ids_json, status_val,
             created_at, updated_at, promoted_at) in cursor.fetchall():
            try:
                fact_ids = json.loads(ids_json) if ids_json else []
            except (ValueError, TypeError):
                fact_ids = []
            out.append({
                "id": rid,
                "summary": summary,
                "entity": entity,
                "relation_type": relation_type,
                "source_fact_ids": fact_ids,
                "status": status_val,
                "created_at": created_at,
                "updated_at": updated_at,
                "promoted_at": promoted_at,
            })
        return out

    def update_reflection_status(
        self,
        reflection_id: int,
        new_status: str,
        *,
        promoted_at: Optional[int] = None,
    ) -> bool:
        """FSM transition. Phase 3b's reconciler is the primary caller;
        the WebUI's promote / deny buttons also route here. Returns
        True iff the row existed and was updated.
        """
        now = int(time.time())
        with self._write_lock:
            conn = self._get_conn()
            if promoted_at is not None:
                cursor = conn.execute(
                    "UPDATE reflections SET status = ?, updated_at = ?, "
                    "promoted_at = ? WHERE id = ?",
                    (new_status, now, int(promoted_at), reflection_id),
                )
            else:
                cursor = conn.execute(
                    "UPDATE reflections SET status = ?, updated_at = ? "
                    "WHERE id = ?",
                    (new_status, now, reflection_id),
                )
            conn.commit()
            return cursor.rowcount > 0

    def list_unabsorbed_events(
        self,
        user_id: str,
        limit: int = 30,
    ) -> List[Tuple[int, str, int, Optional[str]]]:
        """Return events that haven't been folded into a reflection yet.

        Phase 3b's reconciler calls this to assemble the input for
        Stage-2 LLM synthesis. We surface ``importance`` so the
        reflection seed-rein math can read it without a second query.

        Ordering by id DESC favours recent facts — older unabsorbed
        events are usually too stale to combine meaningfully with new
        ones. ``limit`` keeps the LLM prompt bounded.
        """
        limit = max(int(limit), 0)
        conn = self._get_conn()
        cursor = conn.execute(
            "SELECT id, event_summary, importance, tag "
            "FROM event_logs "
            "WHERE user_id = ? AND (absorbed = 0 OR absorbed IS NULL) "
            "ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        )
        return cursor.fetchall()

    def list_promotion_candidates(
        self,
        *,
        age_seconds: float,
        limit: int = 200,
    ) -> List[Dict]:
        """Return pending reflections older than ``age_seconds``.

        Stage-3 reconciler iterates these and decides per-row whether
        to actually promote (evidence + disp checks happen in
        cognition/reconciler.py, not here — the DB doesn't know about
        EvidenceConfig).

        Returns dicts (not tuples) for the same reason
        ``list_reflections`` does — the reconciler reads several
        fields by name, and field-order churn would hurt readability.
        """
        cutoff_epoch = int(time.time() - max(0.0, float(age_seconds)))
        conn = self._get_conn()
        cursor = conn.execute(
            "SELECT id, user_id, summary, entity, relation_type, "
            "       source_fact_ids, status, created_at, updated_at "
            "FROM reflections "
            "WHERE status = 'pending' AND created_at <= ? "
            "ORDER BY created_at ASC LIMIT ?",
            (cutoff_epoch, max(int(limit), 0)),
        )
        out = []
        for (rid, user_id, summary, entity, relation_type, ids_json,
             status_val, created_at, updated_at) in cursor.fetchall():
            try:
                fact_ids = json.loads(ids_json) if ids_json else []
            except (ValueError, TypeError):
                fact_ids = []
            out.append({
                "id": rid,
                "user_id": user_id,
                "summary": summary,
                "entity": entity,
                "relation_type": relation_type,
                "source_fact_ids": fact_ids,
                "status": status_val,
                "created_at": created_at,
                "updated_at": updated_at,
            })
        return out

    def mark_facts_absorbed(self, fact_ids: List[int]) -> int:
        """Bulk-set ``absorbed=1`` on the listed event_logs rows.

        Called by Phase 3b after a reflection consumes a set of facts
        so subsequent reflection-synthesis passes don't double-count
        them. Returns rows affected.
        """
        if not fact_ids:
            return 0
        placeholders = ",".join("?" * len(fact_ids))
        with self._write_lock:
            conn = self._get_conn()
            cursor = conn.execute(
                f"UPDATE event_logs SET absorbed = 1 "
                f"WHERE id IN ({placeholders})",
                fact_ids,
            )
            conn.commit()
            return cursor.rowcount

    def cleanup_old_events(self, user_id: str, keep: int = 50) -> int:
        """Delete oldest events beyond the *keep* threshold. Returns rows deleted."""
        keep = max(keep, 0)
        with self._write_lock:
            conn = self._get_conn()
            cursor = conn.execute(
                """DELETE FROM event_logs
                   WHERE user_id = ? AND id NOT IN (
                       SELECT id FROM event_logs
                       WHERE user_id = ?
                       ORDER BY datetime(created_at) DESC
                       LIMIT ?
                   )""",
                (user_id, user_id, keep)
            )
            conn.commit()
            return cursor.rowcount

    # ── Context Assembly ────────────────────────────────────────────

    @staticmethod
    def _confidence_marker(conf: float) -> str:
        """Return a short marker indicating confidence level."""
        if conf >= 0.8:
            return "✓"
        elif conf >= 0.5:
            return "?"
        else:
            return "~"

    def build_user_context(self, user_id: str, *, max_events: int = 5,
                           max_chars: int = 0,
                           inject_categories: Optional[List[str]] = None,
                           include_events: bool = True,
                           event_tags: Optional[List[str]] = None,
                           hint_other_categories: bool = True) -> str:
        """Assemble a compact memory context for a given user.

        Args:
            inject_categories: Whitelist of category names to inject. ``None``
                means "all categories" (legacy behaviour). Pass e.g.
                ``["basic"]`` to keep token usage low and let the LLM pull
                other categories on demand via the ``memory_query`` tool.
            include_events: Whether to append the recent-events line.
            hint_other_categories: When some categories are filtered out,
                append a one-line hint listing which ones the LLM may query.
        """
        all_profiles = self.get_profiles(user_id)

        if inject_categories is None:
            inject_set: Optional[set] = None
        else:
            inject_set = set(inject_categories)

        # Group all profiles by category so we can both filter and produce a hint.
        by_cat: Dict[str, list] = {}
        for key, value, _, conf, cat, _ in all_profiles:
            by_cat.setdefault(cat, []).append((key, value, conf))

        events = (self.get_recent_events(user_id, limit=max_events, tags=event_tags)
                  if include_events else [])
        if not by_cat and not events:
            return ""

        parts: List[str] = []
        _s = self._sanitize
        injected_cats = set()
        for cat in sorted(by_cat.keys(), key=lambda c: CATEGORY_PRIORITY.get(c, 99)):
            if inject_set is not None and cat not in inject_set:
                continue
            items = by_cat[cat]
            kvs = " | ".join(
                f"{_s(k)}={_s(v)}({self._confidence_marker(c)})" for k, v, c in items
            )
            parts.append(f"[{_s(user_id)}:{_s(cat)}] {kvs}")
            injected_cats.add(cat)

        if events:
            def _fmt(row):
                summary, ts, tag = row
                head = f"{ts[:10]}"
                if tag:
                    head += f"#{_s(tag)}"
                return f"{head} {_s(summary)}"
            evts = " | ".join(_fmt(r) for r in events)
            parts.append(f"[{_s(user_id)}:events] {evts}")

        # Hint the LLM about categories that are present but not injected.
        if hint_other_categories and inject_set is not None:
            other = [c for c in by_cat.keys() if c not in injected_cats]
            if other:
                ordered = sorted(other, key=lambda c: CATEGORY_PRIORITY.get(c, 99))
                parts.append(
                    f"[{_s(user_id)}:hint] 其他记忆类别可用: {', '.join(ordered)} "
                    f"(调用 memory_query(category=...) 查询)"
                )

        result = "\n".join(parts)

        if max_chars > 0 and len(result) > max_chars:
            result = self._truncate_context(parts, max_chars)

        return result

    @staticmethod
    def _truncate_context(parts: list, max_chars: int) -> str:
        """Keep as many lines as fit within the character budget."""
        kept = []
        total = 0
        for line in parts:
            remaining = max_chars - total
            if remaining <= 0:
                break
            cost = len(line) + (1 if kept else 0)
            if cost <= remaining:
                kept.append(line)
                total += cost
            else:
                avail = remaining - (1 if kept else 0)
                if avail > 0:
                    kept.append(line[:avail])
                break
        return "\n".join(kept)

    def get_all_profiles_formatted(self, user_id: str, *, max_events: int = 10,
                                   category: Optional[str] = None) -> str:
        """Full memory dump for memory_query — includes all details.

        If *category* is given, only that category is returned and events are
        omitted (the caller asked for a focused slice).
        """
        if category is not None:
            profiles = self.get_profiles(user_id, include_expired=False, category=category)
            events: list = []
        else:
            profiles = self.get_profiles(user_id, include_expired=False)
            events = self.get_recent_events(user_id, limit=max_events)

        if not profiles and not events:
            if category is not None:
                return f"该用户在分类 [{category}] 下暂无记忆数据。"
            return "该用户暂无记忆数据。"

        lines = []
        if profiles:
            lines.append("【用户画像】" if category is None else f"【用户画像 - {category}】")
            by_cat: Dict[str, list] = {}
            for key, value, _, conf, cat, expires in profiles:
                by_cat.setdefault(cat, []).append((key, value, conf, expires))
            for cat in sorted(by_cat.keys(), key=lambda c: CATEGORY_PRIORITY.get(c, 99)):
                lines.append(f"  [{cat}]")
                for key, value, conf, expires in by_cat[cat]:
                    exp_str = ""
                    iso = _epoch_to_iso_date(expires)
                    if iso:
                        exp_str = f" (过期: {iso})"
                    lines.append(f"    {key} = {value}  [置信度:{conf:.1f}]{exp_str}")

        if events:
            lines.append("【近期事件】")
            for summary, ts, tag in events:
                tag_str = f" [#{tag}]" if tag else ""
                lines.append(f"  {ts[:10]}{tag_str} {summary}")

        return "\n".join(lines)

    # ── Search (M8) ─────────────────────────────────────────────────

    def search_users(self, q: str, limit: int = 100) -> List[Dict]:
        """Find users whose ID, profile values, or event summaries contain *q*.

        Returns a list of ``{user_id, profile_count, event_count, match_in,
        snippet}`` dicts. ``match_in`` is a sorted list of any of
        ``"user_id"``, ``"profile"``, ``"event"`` indicating where the term
        was matched. ``snippet`` is a single short example of the match.
        """
        q = (q or "").strip()
        if not q:
            return []
        # Escape LIKE wildcards so user-supplied terms are treated as literals.
        # Without this, a query like "50%" would match anything containing
        # "50", and a single "%" would match every row. We pick "!" as the
        # escape character (rare in user content) and bind it via ESCAPE '!'
        # in each LIKE clause below. Order matters: escape "!" first so we
        # don't double-escape the escape characters we just inserted.
        q_esc = q.replace("!", "!!").replace("%", "!%").replace("_", "!_")
        like = f"%{q_esc}%"
        conn = self._get_conn()

        # Find candidate user_ids — three sources, unioned, then enriched.
        # Each source aggregates per user_id IN SQL **before** applying the
        # row limit. The naive "LIMIT row matches then dedupe in Python"
        # version was buggy: a single user with N matching rows would consume
        # the whole quota and starve other matching users out of the result.
        rows: dict = {}

        # Match by user_id (already DISTINCT, naturally per-user)
        for (uid,) in conn.execute(
            "SELECT DISTINCT user_id FROM (SELECT user_id FROM user_profiles "
            "UNION SELECT user_id FROM event_logs) WHERE user_id LIKE ? ESCAPE '!' LIMIT ?",
            (like, limit),
        ).fetchall():
            entry = rows.setdefault(uid, {"match_in": set(), "snippet": ""})
            entry["match_in"].add("user_id")
            if not entry["snippet"]:
                entry["snippet"] = f"id: {uid}"

        # Match by profile value. Bug-resistant pattern: aggregating
        # MIN(memory_key) and MIN(memory_value) separately can pick those
        # two strings from *different* rows of the same user, producing a
        # synthetic "key=value" snippet that doesn't actually exist in the DB.
        # Instead, pick a single sample rowid per user via a subquery, then
        # JOIN back to the source row so the snippet's key and value always
        # come from the same record.
        for uid, sample_key, sample_value in conn.execute(
            "SELECT p.user_id, p.memory_key, p.memory_value "
            "FROM user_profiles p "
            "INNER JOIN ("
            "    SELECT user_id, MIN(rowid) AS sample_rowid "
            "    FROM user_profiles "
            "    WHERE memory_value LIKE ? ESCAPE '!' "
            "    GROUP BY user_id "
            "    LIMIT ?"
            ") s ON p.rowid = s.sample_rowid",
            (like, limit),
        ).fetchall():
            entry = rows.setdefault(uid, {"match_in": set(), "snippet": ""})
            entry["match_in"].add("profile")
            if not entry["snippet"]:
                entry["snippet"] = f"{sample_key}={(sample_value or '')[:60]}"

        # Match by event summary — same single-row sampling approach
        for uid, sample_summary in conn.execute(
            "SELECT e.user_id, e.event_summary "
            "FROM event_logs e "
            "INNER JOIN ("
            "    SELECT user_id, MIN(id) AS sample_id "
            "    FROM event_logs "
            "    WHERE event_summary LIKE ? ESCAPE '!' "
            "    GROUP BY user_id "
            "    LIMIT ?"
            ") s ON e.id = s.sample_id",
            (like, limit),
        ).fetchall():
            entry = rows.setdefault(uid, {"match_in": set(), "snippet": ""})
            entry["match_in"].add("event")
            if not entry["snippet"]:
                entry["snippet"] = f"event: {(sample_summary or '')[:60]}"

        if not rows:
            return []

        # Enrich with counts (single round-trip)
        uids = list(rows.keys())
        placeholders = ",".join("?" * len(uids))
        prof_counts = dict(conn.execute(
            f"SELECT user_id, COUNT(*) FROM user_profiles WHERE user_id IN ({placeholders}) GROUP BY user_id",
            uids,
        ).fetchall())
        evt_counts = dict(conn.execute(
            f"SELECT user_id, COUNT(*) FROM event_logs WHERE user_id IN ({placeholders}) GROUP BY user_id",
            uids,
        ).fetchall())

        out = []
        for uid, entry in rows.items():
            out.append({
                "user_id": uid,
                "profile_count": prof_counts.get(uid, 0),
                "event_count": evt_counts.get(uid, 0),
                "match_in": sorted(entry["match_in"]),
                "snippet": entry["snippet"],
            })
        out.sort(key=lambda r: r["user_id"])
        # Three sources can together produce up to 3*limit users — enforce
        # the caller-requested limit at the very end so the contract holds.
        return out[:limit]

    # ── Bulk export / import (M4) ───────────────────────────────────

    EXPORT_SCHEMA_VERSION = 1

    def export_all(self) -> Dict:
        """Dump every user's profiles + events to a JSON-serializable dict.

        Designed to round-trip through ``import_all``.

        Both SELECTs run inside a single deferred transaction so they observe
        the **same** SQLite snapshot — without this guard, a writer that
        commits between the two queries can produce a backup with a row in
        ``event_logs`` referencing a profile that the export has already
        skipped past, leaving cross-table inconsistency in the JSON.
        """
        conn = self._get_conn()
        try:
            conn.execute("BEGIN")
            prof_rows = conn.execute(
                "SELECT user_id, memory_key, memory_value, updated_at, "
                "       confidence, category, expires_at FROM user_profiles"
            ).fetchall()
            evt_rows = conn.execute(
                "SELECT user_id, event_summary, created_at, tag FROM event_logs"
            ).fetchall()
        finally:
            # Release the read lock — read-only so commit() and rollback()
            # are equivalent here.
            try:
                conn.commit()
            except sqlite3.OperationalError:
                # If something went wrong before BEGIN actually opened a
                # transaction, ignore — the SELECTs above will have raised.
                pass
        return {
            "schema_version": self.EXPORT_SCHEMA_VERSION,
            "exported_at": datetime.now().isoformat(),
            "profiles": [
                {
                    "user_id": uid, "key": k, "value": v,
                    "updated_at": ua, "confidence": conf, "category": cat,
                    "expires_at": exp,
                }
                for (uid, k, v, ua, conf, cat, exp) in prof_rows
            ],
            "events": [
                {
                    "user_id": uid, "summary": summary, "created_at": ca, "tag": tag,
                }
                for (uid, summary, ca, tag) in evt_rows
            ],
        }

    def import_all(self, data: Dict, *, mode: str = "merge") -> Dict:
        """Bulk import a snapshot produced by ``export_all``.

        Args:
            data: dict with optional ``profiles`` and ``events`` lists.
            mode: ``"merge"`` (default) keeps existing rows and only adds
                missing ones; ``"replace"`` wipes all data first; ``"upsert"``
                overwrites profiles by (user_id, key) and appends events.

        Returns a dict with counts: ``profiles_added``, ``profiles_updated``,
        ``profiles_skipped``, ``events_added``, ``events_skipped``,
        ``events_skipped_dup``.

        - merge / upsert: lenient — malformed rows are skipped and counted in
          ``profiles_skipped`` (for profiles) or ``events_skipped`` (for events).
        - replace: strict — the snapshot is **fully validated before** the
          existing tables are wiped. Any malformed row raises ``ValueError``
          and the live DB is left untouched. Without this guard, a corrupt
          or partial snapshot would erase the user's data and leave only
          fragments behind.
        """
        if mode not in ("merge", "replace", "upsert"):
            raise ValueError(f"invalid mode: {mode!r}")

        profiles = data.get("profiles") or []
        events = data.get("events") or []
        if not isinstance(profiles, list) or not isinstance(events, list):
            raise ValueError("profiles/events must be lists")

        # Optional schema_version sanity check — only enforce the upper bound
        # (rejecting future versions); older versions are tolerated since
        # they're a strict subset of the current shape.
        sv = data.get("schema_version")
        if sv is not None and isinstance(sv, int) and sv > self.EXPORT_SCHEMA_VERSION:
            raise ValueError(
                f"snapshot schema_version {sv} is newer than supported "
                f"({self.EXPORT_SCHEMA_VERSION}); refusing to import"
            )

        # Strict pre-validation for replace: do a dry run that surfaces every
        # bad row up front so we never DELETE then partially restore.
        if mode == "replace":
            for i, row in enumerate(profiles):
                if not isinstance(row, dict):
                    raise ValueError(
                        f"replace: profiles[{i}] is {type(row).__name__}, expected dict"
                    )
                if not row.get("user_id") or not row.get("key") or row.get("value") is None:
                    raise ValueError(
                        f"replace: profiles[{i}] missing required fields "
                        f"(user_id/key/value)"
                    )
            for i, row in enumerate(events):
                if not isinstance(row, dict):
                    raise ValueError(
                        f"replace: events[{i}] is {type(row).__name__}, expected dict"
                    )
                if not row.get("user_id") or not row.get("summary"):
                    raise ValueError(
                        f"replace: events[{i}] missing required fields "
                        f"(user_id/summary)"
                    )

        # Track profile and event skip counts separately so the return dict
        # can attribute "skipped" to the right table — otherwise a malformed
        # event row would be misreported as ``profiles_skipped`` and confuse
        # the WebUI's import summary during backup/restore.
        added = updated = profiles_skipped = events_skipped = events_added = 0
        with self._write_lock:
            conn = self._get_conn()
            try:
                conn.execute("BEGIN IMMEDIATE")
                if mode == "replace":
                    conn.execute("DELETE FROM user_profiles")
                    conn.execute("DELETE FROM event_logs")

                # Profiles
                for row in profiles:
                    if not isinstance(row, dict):
                        profiles_skipped += 1
                        continue
                    uid = row.get("user_id")
                    key = row.get("key")
                    val = row.get("value")
                    if not uid or not key or val is None:
                        profiles_skipped += 1
                        continue
                    cat = row.get("category", "basic")
                    if cat not in VALID_CATEGORIES:
                        cat = "other"
                    try:
                        conf = max(0.0, min(1.0, float(row.get("confidence", 0.5))))
                    except (TypeError, ValueError):
                        conf = 0.5
                    exp = _to_epoch(row.get("expires_at"))
                    # Normalize updated_at — see _normalize_iso_timestamp;
                    # downstream rendering slices ts[:10] and would crash on
                    # an int/datetime value smuggled in by a hand-edited
                    # snapshot.
                    ua = _normalize_iso_timestamp(row.get("updated_at"))
                    clipped, _ = self._clip_value(val, MAX_PROFILE_VALUE_LEN)

                    if mode == "merge":
                        # `INSERT OR IGNORE` on the (user_id, memory_key) PK
                        # alone would silently drop the imported value when an
                        # existing row exists but is **already expired** —
                        # callers (`get_profiles` / `profile_exists`) treat it
                        # as gone, but the PK is still occupied. Restore data
                        # in that case by overwriting the expired row.
                        now_epoch = int(time.time())
                        existing = conn.execute(
                            "SELECT expires_at FROM user_profiles "
                            "WHERE user_id = ? AND memory_key = ?",
                            (uid, key),
                        ).fetchone()
                        if existing is None:
                            # No collision → straight insert
                            conn.execute(
                                "INSERT INTO user_profiles "
                                "(user_id, memory_key, memory_value, updated_at, confidence, category, expires_at) "
                                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                                (uid, key, clipped, ua, conf, cat, exp),
                            )
                            added += 1
                        else:
                            existing_exp = existing[0]
                            if existing_exp is not None and existing_exp <= now_epoch:
                                # Existing row is expired → revive with the
                                # imported value (other code paths already
                                # treat it as absent, so this isn't surprising).
                                conn.execute(
                                    """UPDATE user_profiles
                                       SET memory_value = ?, updated_at = ?,
                                           confidence = ?, category = ?, expires_at = ?
                                       WHERE user_id = ? AND memory_key = ?""",
                                    (clipped, ua, conf, cat, exp, uid, key),
                                )
                                added += 1
                            else:
                                # Live row already there — preserve it (merge contract)
                                profiles_skipped += 1
                    else:  # replace or upsert
                        cur = conn.execute(
                            """INSERT INTO user_profiles
                                   (user_id, memory_key, memory_value, updated_at, confidence, category, expires_at)
                               VALUES (?, ?, ?, ?, ?, ?, ?)
                               ON CONFLICT(user_id, memory_key)
                               DO UPDATE SET memory_value = excluded.memory_value,
                                             updated_at   = excluded.updated_at,
                                             confidence   = excluded.confidence,
                                             category     = excluded.category,
                                             expires_at   = excluded.expires_at""",
                            (uid, key, clipped, ua, conf, cat, exp),
                        )
                        # We don't easily know if it was insert vs update; count as added
                        added += 1

                # Events:
                #   - replace mode: append all (table was already wiped above)
                #   - merge / upsert: dedupe by natural key
                #     (user_id, created_at, summary, tag) so re-importing the
                #     same snapshot is idempotent. Without this, "merge" would
                #     pollute the timeline with duplicate events on every retry.
                events_skipped_dup = 0
                for row in events:
                    if not isinstance(row, dict):
                        events_skipped += 1
                        continue
                    uid = row.get("user_id")
                    summary = row.get("summary")
                    if not uid or not summary:
                        events_skipped += 1
                        continue
                    # Same normalization as profiles.updated_at — guard
                    # against int epoch / datetime objects in the snapshot.
                    ca = _normalize_iso_timestamp(row.get("created_at"))
                    tag = row.get("tag")
                    if isinstance(tag, str) and not tag.strip():
                        tag = None
                    clipped, _ = self._clip_value(summary, MAX_EVENT_LEN)

                    if mode == "replace":
                        # Table already wiped earlier; just append.
                        conn.execute(
                            "INSERT INTO event_logs (user_id, event_summary, created_at, tag) "
                            "VALUES (?, ?, ?, ?)",
                            (uid, clipped, ca, tag),
                        )
                        events_added += 1
                    else:
                        # Idempotent insert: only write if the natural key isn't
                        # already present. NULL-safe tag comparison via
                        # IS-equivalent expression (SQLite treats NULL = NULL as
                        # NULL by default, which would always be falsy).
                        cur = conn.execute(
                            """INSERT INTO event_logs
                                   (user_id, event_summary, created_at, tag)
                               SELECT ?, ?, ?, ?
                               WHERE NOT EXISTS (
                                   SELECT 1 FROM event_logs
                                   WHERE user_id = ?
                                     AND event_summary = ?
                                     AND created_at = ?
                                     AND ((tag = ?) OR (tag IS NULL AND ? IS NULL))
                               )""",
                            (uid, clipped, ca, tag, uid, clipped, ca, tag, tag),
                        )
                        if cur.rowcount > 0:
                            events_added += 1
                        else:
                            events_skipped_dup += 1
                conn.commit()
            except Exception:
                try:
                    conn.rollback()
                except Exception as rb_err:
                    # See upsert_with_limit's rollback handler for rationale.
                    logger.warning(
                        f"import_all: rollback failed (mode={mode}): {rb_err}"
                    )
                raise

        return {
            "mode": mode,
            "profiles_added": added,
            "profiles_updated": updated,
            "profiles_skipped": profiles_skipped,
            "events_added": events_added,
            "events_skipped": events_skipped,
            # Events that matched an existing row by natural key — only
            # populated for merge/upsert modes; always 0 for replace.
            "events_skipped_dup": events_skipped_dup if mode != "replace" else 0,
        }
