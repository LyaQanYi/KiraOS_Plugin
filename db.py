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

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._tls = threading.local()
        # Registry of every connection ever handed out (one per thread that
        # touched the DB), so close() can close all of them.
        self._conn_registry: List[sqlite3.Connection] = []
        self._registry_lock = Lock()
        self._write_lock = Lock()
        self._closed = False
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

        The "check closed → connect → register" sequence is performed under
        ``_registry_lock`` so a concurrent ``close()`` cannot slip in between
        the closed-check and the registry append. Without the guard, a
        connection created right after ``close()`` finished would be left
        unregistered and never closed (resource leak).
        """
        conn = getattr(self._tls, "conn", None)
        if conn is None:
            with self._registry_lock:
                # Re-check under the lock in case another thread on the same
                # tls instance raced (shouldn't happen with thread-local but
                # cheap to verify and harmless if redundant).
                conn = getattr(self._tls, "conn", None)
                if conn is None:
                    if self._closed:
                        raise RuntimeError("UserMemoryDB has been closed")
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
            logger.info(f"User memory database initialized at {self.db_path}")

    def _migrate(self, conn: sqlite3.Connection):
        """Auto-add new columns and rewrite legacy ISO-string expires_at values."""
        cursor = conn.execute("PRAGMA table_info(user_profiles)")
        existing_cols = {row[1] for row in cursor.fetchall()}
        migrations = [
            ("confidence", "REAL DEFAULT 0.5"),
            ("category", "TEXT DEFAULT 'basic'"),
            ("expires_at", "INTEGER DEFAULT NULL"),
        ]
        for col_name, col_def in migrations:
            if col_name not in existing_cols:
                conn.execute(f"ALTER TABLE user_profiles ADD COLUMN {col_name} {col_def}")
                logger.info(f"Migrated user_profiles: added column '{col_name}'")
        conn.commit()

        # event_logs.tag was added in this revision
        cursor = conn.execute("PRAGMA table_info(event_logs)")
        event_cols = {row[1] for row in cursor.fetchall()}
        if "tag" not in event_cols:
            conn.execute("ALTER TABLE event_logs ADD COLUMN tag TEXT DEFAULT NULL")
            logger.info("Migrated event_logs: added column 'tag'")
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
                f"Profile value for {user_id}/{key} truncated from {len(str(value))} "
                f"to {MAX_PROFILE_VALUE_LEN} chars"
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
                        f"{user_id}/{key}: {rb_err}"
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
                f"Event summary for {user_id} truncated from {len(str(event_summary))} "
                f"to {MAX_EVENT_LEN} chars"
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
        rows: dict = {}

        # match by user_id
        for (uid,) in conn.execute(
            "SELECT DISTINCT user_id FROM (SELECT user_id FROM user_profiles "
            "UNION SELECT user_id FROM event_logs) WHERE user_id LIKE ? ESCAPE '!' LIMIT ?",
            (like, limit),
        ).fetchall():
            entry = rows.setdefault(uid, {"match_in": set(), "snippet": ""})
            entry["match_in"].add("user_id")
            if not entry["snippet"]:
                entry["snippet"] = f"id: {uid}"

        # match by profile value
        for uid, key, value in conn.execute(
            "SELECT user_id, memory_key, memory_value FROM user_profiles "
            "WHERE memory_value LIKE ? ESCAPE '!' LIMIT ?",
            (like, limit),
        ).fetchall():
            entry = rows.setdefault(uid, {"match_in": set(), "snippet": ""})
            entry["match_in"].add("profile")
            if not entry["snippet"]:
                entry["snippet"] = f"{key}={value[:60]}"

        # match by event summary
        for uid, summary in conn.execute(
            "SELECT user_id, event_summary FROM event_logs "
            "WHERE event_summary LIKE ? ESCAPE '!' LIMIT ?",
            (like, limit),
        ).fetchall():
            entry = rows.setdefault(uid, {"match_in": set(), "snippet": ""})
            entry["match_in"].add("event")
            if not entry["snippet"]:
                entry["snippet"] = f"event: {summary[:60]}"

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
        return out

    # ── Bulk export / import (M4) ───────────────────────────────────

    EXPORT_SCHEMA_VERSION = 1

    def export_all(self) -> Dict:
        """Dump every user's profiles + events to a JSON-serializable dict.

        Designed to round-trip through ``import_all``.
        """
        conn = self._get_conn()
        prof_rows = conn.execute(
            "SELECT user_id, memory_key, memory_value, updated_at, "
            "       confidence, category, expires_at FROM user_profiles"
        ).fetchall()
        evt_rows = conn.execute(
            "SELECT user_id, event_summary, created_at, tag FROM event_logs"
        ).fetchall()
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
        ``profiles_skipped``, ``events_added``.

        Validation is permissive — malformed rows are skipped with a warning
        rather than aborting the whole import.
        """
        if mode not in ("merge", "replace", "upsert"):
            raise ValueError(f"invalid mode: {mode!r}")

        profiles = data.get("profiles") or []
        events = data.get("events") or []
        if not isinstance(profiles, list) or not isinstance(events, list):
            raise ValueError("profiles/events must be lists")

        added = updated = skipped = events_added = 0
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
                        skipped += 1
                        continue
                    uid = row.get("user_id"); key = row.get("key"); val = row.get("value")
                    if not uid or not key or val is None:
                        skipped += 1
                        continue
                    cat = row.get("category", "basic")
                    if cat not in VALID_CATEGORIES:
                        cat = "other"
                    try:
                        conf = max(0.0, min(1.0, float(row.get("confidence", 0.5))))
                    except (TypeError, ValueError):
                        conf = 0.5
                    exp = _to_epoch(row.get("expires_at"))
                    ua = row.get("updated_at") or datetime.now().isoformat()
                    clipped, _ = self._clip_value(val, MAX_PROFILE_VALUE_LEN)

                    if mode == "merge":
                        cur = conn.execute(
                            "INSERT OR IGNORE INTO user_profiles "
                            "(user_id, memory_key, memory_value, updated_at, confidence, category, expires_at) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?)",
                            (uid, key, clipped, ua, conf, cat, exp),
                        )
                        if cur.rowcount > 0:
                            added += 1
                        else:
                            skipped += 1
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
                        skipped += 1
                        continue
                    uid = row.get("user_id"); summary = row.get("summary")
                    if not uid or not summary:
                        skipped += 1
                        continue
                    ca = row.get("created_at") or datetime.now().isoformat()
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
            "profiles_skipped": skipped,
            "events_added": events_added,
            # Events that matched an existing row by natural key — only
            # populated for merge/upsert modes; always 0 for replace.
            "events_skipped_dup": events_skipped_dup if mode != "replace" else 0,
        }
