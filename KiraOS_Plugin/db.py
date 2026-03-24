"""
SQLite-based user memory storage for KiraAI.

Tables:
  - user_profiles: Long-term key-value user profile entries
                   with confidence, category, and optional expiration
  - event_logs:    Recent event logs per user
"""

import sqlite3
import os
import re
from datetime import datetime, timedelta
from typing import List, Tuple, Optional, Dict
from threading import Lock

from core.logging_manager import get_logger

logger = get_logger("kiraos_db", "green")

# Category priority for context injection (lower = higher priority)
CATEGORY_PRIORITY = {"basic": 0, "preference": 1, "social": 2, "other": 3}
VALID_CATEGORIES = set(CATEGORY_PRIORITY.keys())


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


class UserMemoryDB:
    """Thread-safe SQLite wrapper for user memory persistence."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._lock = Lock()
        self._ensure_dir()
        self._conn: sqlite3.Connection | None = None
        self._init_db()
    def _ensure_dir(self):
        dir_path = os.path.dirname(self.db_path)
        if dir_path:
            os.makedirs(dir_path, exist_ok=True)

    def _get_conn(self) -> sqlite3.Connection:
        """Return (and lazily create) the shared connection.

        **Caller must hold ``self._lock``** before invoking this method.
        """
        if self._conn is None:
            self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
        return self._conn

    def close(self):
        """Close the persistent connection."""
        with self._lock:
            if self._conn:
                self._conn.close()
                self._conn = None

    def _init_db(self):
        """Create tables if they don't exist, and migrate old schemas."""
        with self._lock:
            conn = self._get_conn()
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS user_profiles (
                    user_id      TEXT NOT NULL,
                    memory_key   TEXT NOT NULL,
                    memory_value TEXT NOT NULL,
                    updated_at   DATETIME DEFAULT CURRENT_TIMESTAMP,
                    confidence   REAL DEFAULT 0.5,
                    category     TEXT DEFAULT 'basic',
                    expires_at   DATETIME DEFAULT NULL,
                    PRIMARY KEY (user_id, memory_key)
                );

                CREATE TABLE IF NOT EXISTS event_logs (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id       TEXT NOT NULL,
                    event_summary TEXT NOT NULL,
                    created_at    DATETIME DEFAULT CURRENT_TIMESTAMP
                );

                CREATE INDEX IF NOT EXISTS idx_event_logs_user
                    ON event_logs (user_id, created_at DESC);
            """)
            conn.commit()
            self._migrate(conn)
            logger.info(f"User memory database initialized at {self.db_path}")

    def _migrate(self, conn: sqlite3.Connection):
        """Auto-add new columns to old tables (idempotent)."""
        cursor = conn.execute("PRAGMA table_info(user_profiles)")
        existing_cols = {row[1] for row in cursor.fetchall()}
        migrations = [
            ("confidence", "REAL DEFAULT 0.5"),
            ("category", "TEXT DEFAULT 'basic'"),
            ("expires_at", "DATETIME DEFAULT NULL"),
        ]
        for col_name, col_def in migrations:
            if col_name not in existing_cols:
                conn.execute(f"ALTER TABLE user_profiles ADD COLUMN {col_name} {col_def}")
                logger.info(f"Migrated user_profiles: added column '{col_name}'")
        conn.commit()
    # ── Profile Operations ──────────────────────────────────────────

    def save_profile(self, user_id: str, key: str, value: str, *,
                     confidence: float = 0.5, category: str = "basic",
                     expires_at: Optional[str] = None) -> None:
        """Insert or update a user profile entry with metadata."""
        if category not in VALID_CATEGORIES:
            category = "other"
        confidence = max(0.0, min(1.0, confidence))
        with self._lock:
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
                (user_id, key, value, datetime.now().isoformat(),
                 confidence, category, expires_at)
            )
            conn.commit()

    def get_profiles(self, user_id: str, *, include_expired: bool = False
                     ) -> List[Tuple[str, str, str, float, str, Optional[str]]]:
        """Return profile entries as (key, value, updated_at, confidence, category, expires_at).

        By default, expired entries are filtered out.
        """
        now = datetime.now().isoformat()
        with self._lock:
            conn = self._get_conn()
            if include_expired:
                sql = """SELECT memory_key, memory_value, updated_at, confidence, category, expires_at
                         FROM user_profiles WHERE user_id = ? ORDER BY updated_at DESC"""
                cursor = conn.execute(sql, (user_id,))
            else:
                sql = """SELECT memory_key, memory_value, updated_at, confidence, category, expires_at
                         FROM user_profiles
                         WHERE user_id = ? AND (expires_at IS NULL OR expires_at > ?)
                         ORDER BY updated_at DESC"""
                cursor = conn.execute(sql, (user_id, now))
            return cursor.fetchall()

    def get_profiles_by_category(self, user_id: str, category: str
                                 ) -> List[Tuple[str, str, float]]:
        """Return (key, value, confidence) for a specific category, excluding expired."""
        now = datetime.now().isoformat()
        with self._lock:
            conn = self._get_conn()
            cursor = conn.execute(
                """SELECT memory_key, memory_value, confidence FROM user_profiles
                   WHERE user_id = ? AND category = ?
                     AND (expires_at IS NULL OR expires_at > ?)
                   ORDER BY confidence DESC, updated_at DESC""",
                (user_id, category, now)
            )
            return cursor.fetchall()
    def remove_profile(self, user_id: str, key: str) -> bool:
        """Remove a specific profile entry. Returns True if a row was deleted."""
        with self._lock:
            conn = self._get_conn()
            cursor = conn.execute(
                "DELETE FROM user_profiles WHERE user_id = ? AND memory_key = ?",
                (user_id, key)
            )
            conn.commit()
            return cursor.rowcount > 0

    def profile_exists(self, user_id: str, key: str) -> bool:
        """Return True if a non-expired profile entry exists."""
        now = datetime.now().isoformat()
        with self._lock:
            conn = self._get_conn()
            cursor = conn.execute(
                """SELECT 1 FROM user_profiles
                   WHERE user_id = ? AND memory_key = ?
                     AND (expires_at IS NULL OR expires_at > ?)
                   LIMIT 1""",
                (user_id, key, now)
            )
            return cursor.fetchone() is not None

    def get_profile_count(self, user_id: str) -> int:
        """Return the number of non-expired profile entries for a user."""
        now = datetime.now().isoformat()
        with self._lock:
            conn = self._get_conn()
            cursor = conn.execute(
                """SELECT COUNT(*) FROM user_profiles
                   WHERE user_id = ? AND (expires_at IS NULL OR expires_at > ?)""",
                (user_id, now)
            )
            return cursor.fetchone()[0]

    def clear_user_memory(self, user_id: str) -> Tuple[int, int]:
        """Delete all profiles and events for a user. Returns (profiles_deleted, events_deleted)."""
        with self._lock:
            conn = self._get_conn()
            c1 = conn.execute("DELETE FROM user_profiles WHERE user_id = ?", (user_id,))
            c2 = conn.execute("DELETE FROM event_logs WHERE user_id = ?", (user_id,))
            conn.commit()
            return c1.rowcount, c2.rowcount
    # ── Event Log Operations ────────────────────────────────────────

    def save_event(self, user_id: str, event_summary: str) -> None:
        """Append an event log entry."""
        with self._lock:
            conn = self._get_conn()
            conn.execute(
                "INSERT INTO event_logs (user_id, event_summary, created_at) VALUES (?, ?, ?)",
                (user_id, event_summary, datetime.now().isoformat())
            )
            conn.commit()

    def get_recent_events(self, user_id: str, limit: int = 5) -> List[Tuple[str, str]]:
        """Return the most recent events for a user as (event_summary, created_at) tuples."""
        limit = max(limit, 0)
        with self._lock:
            conn = self._get_conn()
            cursor = conn.execute(
                "SELECT event_summary, created_at FROM event_logs WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
                (user_id, limit)
            )
            return cursor.fetchall()

    def get_event_count(self, user_id: str) -> int:
        """Return the number of event logs for a user."""
        with self._lock:
            conn = self._get_conn()
            cursor = conn.execute(
                "SELECT COUNT(*) FROM event_logs WHERE user_id = ?",
                (user_id,)
            )
            return cursor.fetchone()[0]

    def cleanup_old_events(self, user_id: str, keep: int = 50) -> int:
        """Delete oldest events beyond the *keep* threshold. Returns rows deleted."""
        keep = max(keep, 0)
        with self._lock:
            conn = self._get_conn()
            cursor = conn.execute(
                """DELETE FROM event_logs
                   WHERE user_id = ? AND id NOT IN (
                       SELECT id FROM event_logs
                       WHERE user_id = ?
                       ORDER BY created_at DESC
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

    def build_user_context(self, user_id: str, max_events: int = 5,
                           max_chars: int = 0) -> str:
        """
        Assemble a compact memory context for a given user.
        Organized by category with confidence markers.
        Respects max_chars budget (0 = unlimited).
        """
        profiles = self.get_profiles(user_id)
        events = self.get_recent_events(user_id, limit=max_events)

        if not profiles and not events:
            return ""

        # Group profiles by category
        by_cat: Dict[str, list] = {}
        for key, value, _, conf, cat, _ in profiles:
            by_cat.setdefault(cat, []).append((key, value, conf))

        parts = []
        # Emit categories in priority order
        for cat in sorted(by_cat.keys(), key=lambda c: CATEGORY_PRIORITY.get(c, 99)):
            items = by_cat[cat]
            kvs = " | ".join(
                f"{k}={v}({self._confidence_marker(c)})" for k, v, c in items
            )
            parts.append(f"[{user_id}:{cat}] {kvs}")

        if events:
            evts = " | ".join(f"{ts[:10]} {s}" for s, ts in events)
            parts.append(f"[{user_id}:events] {evts}")

        result = "\n".join(parts)

        # Truncate by category priority if over budget
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
            cost = len(line) + (1 if kept else 0)  # +1 for newline separator
            if cost <= remaining:
                kept.append(line)
                total += cost
            else:
                # Truncate the line to fit the remaining budget
                avail = remaining - (1 if kept else 0)
                if avail > 0:
                    kept.append(line[:avail])
                break
        return "\n".join(kept)

    def get_all_profiles_formatted(self, user_id: str, max_events: int = 10) -> str:
        """Full memory dump for memory_query — includes all details."""
        profiles = self.get_profiles(user_id, include_expired=False)
        events = self.get_recent_events(user_id, limit=max_events)

        if not profiles and not events:
            return "该用户暂无记忆数据。"

        lines = []
        if profiles:
            lines.append("【用户画像】")
            by_cat: Dict[str, list] = {}
            for key, value, _, conf, cat, expires in profiles:
                by_cat.setdefault(cat, []).append((key, value, conf, expires))
            for cat in sorted(by_cat.keys(), key=lambda c: CATEGORY_PRIORITY.get(c, 99)):
                lines.append(f"  [{cat}]")
                for key, value, conf, expires in by_cat[cat]:
                    exp_str = f" (过期: {expires[:10]})" if expires else ""
                    lines.append(f"    {key} = {value}  [置信度:{conf:.1f}]{exp_str}")

        if events:
            lines.append("【近期事件】")
            for summary, ts in events:
                lines.append(f"  {ts[:10]} {summary}")

        return "\n".join(lines)
