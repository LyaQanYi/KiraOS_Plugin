"""Import MaiBot / QanMoCore memory into KiraOS.

This module is a one-shot, read-only importer for MaiBot (MaiCore/QanMoCore)
data. It is deliberately independent from the hippocampus pipeline: no LLM
calls, no network access, and no source modification. It writes the KiraOS
TOML truth-source layout directly; the plugin rebuilds its SQLite index from
TOML on the next start.

Import records are deterministic and idempotent. A hidden manifest
(<memory_root>/.qanmocore_migrated, intentionally without a .json suffix so
the index rebuild does not parse it as a legacy JSON memory) records the
per-item state and content hashes:

    imported / updated  -> owned by this importer, safe to update on rerun
    edited / conflict   -> the user changed or replaced the memory; never overwrite
    deleted             -> the user removed it; never resurrect

Only memories tagged with qanmocore-import are ever updated. A user-created
memory with a colliding id is left untouched.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Tuple

from core.logging_manager import get_logger

from .entity_profile import EntityProfile, EntityProfileStore
from .memory_index import normalize_tags
from .memory_paths import (
    ENTITY_GROUP,
    ENTITY_USER,
    ensure_directory_structure,
    get_entity_profile_path,
    get_memory_root,
)
from .toml_tree_store import TomlTreeStore

logger = get_logger("kiraos_qanmocore_migration", "cyan")

MANIFEST_NAME = ".qanmocore_migrated"
MANIFEST_VERSION = 1
IMPORT_TAG = "qanmocore-import"

# Source categories accepted by --include / --exclude.
CATEGORY_PROFILES = "profiles"
CATEGORY_HISTORY = "history"
CATEGORY_CHEST = "chest"
CATEGORY_JARGON = "jargon"
CATEGORY_QA = "qa"
CATEGORY_CONFLICTS = "conflicts"
CATEGORY_MEMORY = "memory"
CATEGORY_HIPPO = "hippo-topics"

DEFAULT_INCLUDE = (
    CATEGORY_PROFILES,
    CATEGORY_HISTORY,
    CATEGORY_CHEST,
    CATEGORY_JARGON,
    CATEGORY_QA,
    CATEGORY_CONFLICTS,
    CATEGORY_MEMORY,
)
ALL_CATEGORIES = DEFAULT_INCLUDE + (CATEGORY_HIPPO,)

_CATEGORY_ALIASES = {
    "profiles": CATEGORY_PROFILES,
    "profile": CATEGORY_PROFILES,
    "person_info": CATEGORY_PROFILES,
    "history": CATEGORY_HISTORY,
    "chat_history": CATEGORY_HISTORY,
    "chest": CATEGORY_CHEST,
    "memory_chest": CATEGORY_CHEST,
    "jargon": CATEGORY_JARGON,
    "qa": CATEGORY_QA,
    "thinking_back": CATEGORY_QA,
    "thinking-back": CATEGORY_QA,
    "conflicts": CATEGORY_CONFLICTS,
    "memory_conflicts": CATEGORY_CONFLICTS,
    "memory": CATEGORY_MEMORY,
    "hippo": CATEGORY_HIPPO,
    "hippo-topics": CATEGORY_HIPPO,
    "hippo_topics": CATEGORY_HIPPO,
}

_NEGATIVE_QA_PREFIXES = (
    "根据现有信息",
    "未找到",
    "无法确定",
    "无法确认",
    "无法判断",
)

def _iso(ts: Optional[float] = None) -> str:
    if ts is None or ts <= 0:
        ts = time.time()
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return datetime.now(tz=timezone.utc).isoformat()


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    s = str(value).replace("\r\n", "\n").replace("\r", "\n").strip()
    if not s or s.lower() == "none" or s.lower() == "null":
        return ""
    return s


def _text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _short_hash(text: str) -> str:
    return _text_hash(text)[:8]


def _payload_hash(payload: Any) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or isinstance(value, bool):
            return default
        result = float(value)
    except (TypeError, ValueError):
        return default
    if result != result or result in (float("inf"), float("-inf")):
        return default
    return result


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or isinstance(value, bool):
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _json_list(value: Any) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    text = str(value).strip()
    if not text or text.lower() in ("none", "null"):
        return []
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return []
    return data if isinstance(data, list) else []


def _toml_safe(value: Any) -> Any:
    """Strip None and coerce values to types tomli_w can serialise."""
    if value is None:
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple)):
        out = []
        for item in value:
            clean = _toml_safe(item)
            if clean is not None:
                out.append(clean)
        return out
    if isinstance(value, dict):
        out: Dict[str, Any] = {}
        for key, item in value.items():
            clean = _toml_safe(item)
            if clean is not None:
                out[str(key)] = clean
        return out
    return str(value)


def _merge_tags(*groups: Any) -> list:
    tags: list = []
    for group in groups:
        for tag in group or []:
            if not isinstance(tag, str):
                tag = str(tag)
            tag = tag.strip()
            if tag and tag not in tags:
                tags.append(tag)
    return normalize_tags(tags)


def _normalize_categories(values: Iterable[str]) -> set:
    out = set()
    for value in values:
        for part in str(value).replace(";", ",").split(","):
            key = part.strip().lower()
            if not key:
                continue
            canonical = _CATEGORY_ALIASES.get(key, key)
            out.add(canonical)
    return out


@dataclass
class ImportOptions:
    """Knobs shared by the CLI and the tests."""

    adapter: str = "qq"
    include: set = field(default_factory=lambda: set(DEFAULT_INCLUDE))
    profile_facts_limit: int = 12
    profile_facts_max_chars: int = 1200
    profile_fact_clip: int = 200
    since: Optional[float] = None
    until: Optional[float] = None
    max_per_chat: Optional[int] = None
    keep_unanswered: bool = False
    force: bool = False
    dry_run: bool = False
    manifest_path: Optional[Path] = None
    progress: Optional[Callable[[str, int, int], None]] = None
    now: Optional[float] = None

    def __post_init__(self) -> None:
        self.include = _normalize_categories(self.include or DEFAULT_INCLUDE)
        unknown = self.include - set(ALL_CATEGORIES)
        if unknown:
            logger.warning(
                "ignoring unknown import categories: %s", ", ".join(sorted(unknown))
            )
            self.include -= unknown
        self.profile_facts_limit = max(1, int(self.profile_facts_limit))
        self.profile_facts_max_chars = max(50, int(self.profile_facts_max_chars))
        self.profile_fact_clip = max(20, int(self.profile_fact_clip))
        if self.max_per_chat is not None:
            self.max_per_chat = max(1, int(self.max_per_chat))


@dataclass
class ImportStats:
    dry_run: bool = False
    imported: Dict[str, int] = field(default_factory=dict)
    updated: Dict[str, int] = field(default_factory=dict)
    skipped: Dict[str, int] = field(default_factory=dict)
    profiles_written: int = 0
    errors: List[str] = field(default_factory=list)
    elapsed: float = 0.0
    output_root: str = ""
    manifest_path: str = ""
    source: Dict[str, Any] = field(default_factory=dict)

    def add_imported(self, category: str, n: int = 1) -> None:
        self.imported[category] = self.imported.get(category, 0) + n

    def add_updated(self, category: str, n: int = 1) -> None:
        self.updated[category] = self.updated.get(category, 0) + n

    def skip(self, reason: str, n: int = 1) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + n

    def error(self, item: str, exc: Exception) -> None:
        if len(self.errors) >= 100:
            return
        self.errors.append(f"{item}: {type(exc).__name__}: {exc}")
        logger.warning("QanMoCore import failed for %s: %s", item, exc)

    @property
    def total_imported(self) -> int:
        return sum(self.imported.values())

    @property
    def total_updated(self) -> int:
        return sum(self.updated.values())

    def to_dict(self) -> Dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "imported": dict(sorted(self.imported.items())),
            "updated": dict(sorted(self.updated.items())),
            "skipped": dict(sorted(self.skipped.items())),
            "profiles_written": self.profiles_written,
            "errors": list(self.errors),
            "elapsed_seconds": round(self.elapsed, 2),
            "output_root": self.output_root,
            "manifest_path": self.manifest_path,
            "source": self.source,
        }


# ── Manifest ────────────────────────────────────────────────────────────────


def _fresh_manifest() -> Dict[str, Any]:
    return {
        "version": MANIFEST_VERSION,
        "items": {},
        "profiles": {},
        "source": {},
        "run_at": None,
        "imported_at": None,
    }


def load_manifest(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return _fresh_manifest()
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RuntimeError(f"cannot read QanMoCore import manifest {path}: {exc}") from exc
    if not raw:
        return _fresh_manifest()
    try:
        data = json.loads(raw)
    except ValueError as exc:
        backup = path.with_name(path.name + f".corrupt_{int(time.time())}")
        try:
            path.replace(backup)
        except OSError:
            pass
        logger.warning(
            "corrupt QanMoCore import manifest %s (%s); backed up to %s and starting fresh",
            path,
            exc,
            backup,
        )
        return _fresh_manifest()
    if not isinstance(data, dict):
        raise RuntimeError(f"unrecognised QanMoCore import manifest {path}")
    version = data.get("version")
    if version != MANIFEST_VERSION:
        logger.warning(
            "unsupported QanMoCore import manifest version %r in %s; starting fresh",
            version,
            path,
        )
        return _fresh_manifest()
    manifest = _fresh_manifest()
    for key in ("items", "profiles"):
        value = data.get(key)
        if isinstance(value, dict):
            manifest[key] = {
                str(k): v for k, v in value.items() if isinstance(v, dict)
            }
    source = data.get("source")
    if isinstance(source, dict):
        manifest["source"] = source
    manifest["run_at"] = data.get("run_at")
    imported_at = data.get("imported_at")
    if isinstance(imported_at, (int, float)) and not isinstance(imported_at, bool):
        manifest["imported_at"] = float(imported_at)
    return manifest


def write_manifest(path: Path, manifest: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    payload = json.dumps(manifest, ensure_ascii=False, indent=2, default=str)
    try:
        with tmp.open("w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        raise


# ── Source access ───────────────────────────────────────────────────────────


class QanMoCoreSource:
    """Read-only view of a MaiBot / QanMoCore data directory."""

    def __init__(self, source_dir: Path, *, adapter: str = "qq", allow_live: bool = False):
        self.source_dir = Path(source_dir)
        self.db_path = self.source_dir / "MaiBot.db"
        self.adapter = adapter
        self.allow_live = allow_live
        self.conn: Optional[sqlite3.Connection] = None
        self.tables: set = set()
        self.streams: Dict[str, dict] = {}
        self.name_map: Dict[str, Dict[str, str]] = {"g": {}, "u": {}}
        self.history_counts: Dict[str, int] = {}
        self.qa_counts: Dict[str, int] = {}

    # -- context manager ---------------------------------------------------

    def __enter__(self) -> "QanMoCoreSource":
        self.open()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # -- lifecycle ---------------------------------------------------------

    def open(self) -> None:
        if not self.source_dir.is_dir():
            raise FileNotFoundError(f"QanMoCore data directory not found: {self.source_dir}")
        if not self.db_path.is_file():
            raise FileNotFoundError(f"MaiBot.db not found under {self.source_dir}")

        wal = Path(str(self.db_path) + "-wal")
        has_live_wal = wal.exists() and wal.stat().st_size > 0
        if has_live_wal and not self.allow_live:
            raise RuntimeError(
                "MaiBot.db-wal is non-empty; the old bot may still be running. "
                "Stop it first, or pass --allow-live-source to read anyway."
            )
        queries = ["mode=ro"] if has_live_wal else ["mode=ro&immutable=1", "mode=ro"]
        last_error: Optional[Exception] = None
        conn: Optional[sqlite3.Connection] = None
        for query in queries:
            candidate: Optional[sqlite3.Connection] = None
            try:
                candidate = sqlite3.connect(
                    f"{self.db_path.as_uri()}?{query}", uri=True, timeout=30
                )
                candidate.row_factory = sqlite3.Row
                candidate.execute("PRAGMA query_only=ON")
                candidate.execute("SELECT 1")
                conn = candidate
                break
            except sqlite3.Error as exc:
                last_error = exc
                if candidate is not None:
                    try:
                        candidate.close()
                    except Exception:
                        pass
        if conn is None:
            raise RuntimeError(f"cannot open MaiBot.db read-only: {last_error}")
        self.conn = conn
        self.tables = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
            )
        }
        self.streams = self._load_streams()
        self.name_map = self._load_name_map()
        self.history_counts = self._count_by_chat("chat_history")
        self.qa_counts = self._count_by_chat("thinking_back")
        logger.info(
            "QanMoCore source opened: db=%s tables=%d streams=%d (live_wal=%s)",
            self.db_path,
            len(self.tables),
            len(self.streams),
            has_live_wal,
        )

    def close(self) -> None:
        if self.conn is not None:
            try:
                self.conn.close()
            finally:
                self.conn = None

    # -- helpers -----------------------------------------------------------

    def has_table(self, name: str) -> bool:
        return name in self.tables

    def count(self, table: str) -> int:
        if self.conn is None or not self.has_table(table):
            return 0
        try:
            row = self.conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()
            return int(row[0]) if row else 0
        except sqlite3.Error:
            return 0

    def _rows(self, table: str, columns: str, order_by: str = "") -> List[dict]:
        if self.conn is None or not self.has_table(table):
            return []
        sql = f'SELECT {columns} FROM "{table}"'
        if order_by:
            sql += f" ORDER BY {order_by}"
        try:
            return [dict(row) for row in self.conn.execute(sql)]
        except sqlite3.Error as exc:
            logger.warning("QanMoCore table %s unreadable: %s", table, exc)
            return []

    def _count_by_chat(self, table: str) -> Dict[str, int]:
        if self.conn is None or not self.has_table(table):
            return {}
        try:
            rows = self.conn.execute(
                f'SELECT chat_id, COUNT(*) FROM "{table}" GROUP BY chat_id'
            ).fetchall()
            return {str(row[0]): int(row[1]) for row in rows if row[0]}
        except sqlite3.Error:
            return {}

    def _load_streams(self) -> Dict[str, dict]:
        if not self.has_table("chat_streams"):
            return {}
        rows = self._rows(
            "chat_streams",
            "stream_id, platform, group_id, group_name, user_id, "
            "user_nickname, last_active_time",
        )
        out: Dict[str, dict] = {}
        for row in rows:
            stream_id = row.get("stream_id")
            if stream_id:
                out[str(stream_id)] = row
        return out

    def _load_name_map(self) -> Dict[str, Dict[str, str]]:
        path = self.source_dir / "local_store.json"
        out: Dict[str, Dict[str, str]] = {"g": {}, "u": {}}
        if not path.is_file():
            return out
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("cannot read local_store.json (%s); names fall back to chat_streams", exc)
            return out
        mapping = None
        if isinstance(data, dict):
            stats = data.get("last_full_statistics")
            if isinstance(stats, dict):
                mapping = stats.get("name_mapping")
        if not isinstance(mapping, dict):
            return out
        for key, value in mapping.items():
            if not isinstance(key, str) or len(key) < 2:
                continue
            prefix = key[0]
            if prefix not in ("g", "u"):
                continue
            name = ""
            if isinstance(value, (list, tuple)) and value:
                name = value[0]
            elif isinstance(value, str):
                name = value
            if isinstance(name, str) and name.strip():
                out[prefix][key[1:]] = name.strip()
        return out

    # -- entity mapping ----------------------------------------------------

    def entity_for_stream(self, chat_id: Any) -> Optional[Tuple[str, str, str]]:
        """Return (entity_id, entity_type, session) for a chat stream id."""
        stream = self.streams.get(str(chat_id or ""))
        if not stream:
            return None
        group_id = _clean_text(stream.get("group_id"))
        if group_id:
            return (
                f"{self.adapter}:{group_id}",
                ENTITY_GROUP,
                f"{self.adapter}:gm:{group_id}",
            )
        user_id = _clean_text(stream.get("user_id"))
        if not user_id:
            return None
        return (
            f"{self.adapter}:{user_id}",
            ENTITY_USER,
            f"{self.adapter}:dm:{user_id}",
        )

    def group_streams(self) -> List[dict]:
        return [
            stream
            for stream in self.streams.values()
            if _clean_text(stream.get("group_id"))
        ]

    def private_streams(self) -> List[dict]:
        return [
            stream
            for stream in self.streams.values()
            if not _clean_text(stream.get("group_id")) and _clean_text(stream.get("user_id"))
        ]

    # -- table iterators ---------------------------------------------------

    def iter_person_info(self) -> Iterator[dict]:
        yield from self._rows(
            "person_info",
            "id, is_known, person_id, person_name, name_reason, platform, "
            "user_id, nickname, memory_points, know_times, know_since, "
            "last_know, group_nick_name",
            "id ASC",
        )

    def iter_chat_history(self, newest_first: bool = False) -> Iterator[dict]:
        order = "end_time DESC, id DESC" if newest_first else "id ASC"
        yield from self._rows(
            "chat_history",
            "id, chat_id, start_time, end_time, participants, theme, keywords, "
            "summary, count, forget_times, key_point",
            order,
        )

    def iter_chest(self) -> Iterator[dict]:
        yield from self._rows(
            "memory_chest",
            "id, title, content, chat_id, locked",
            "id ASC",
        )

    def iter_jargon(self) -> Iterator[dict]:
        yield from self._rows(
            "jargon",
            "id, content, meaning, chat_id, is_global, count, is_jargon, "
            "is_complete, inference_with_context, inference_content_only",
            "id ASC",
        )

    def iter_qa(self, newest_first: bool = False) -> Iterator[dict]:
        order = "create_time DESC, id DESC" if newest_first else "id ASC"
        yield from self._rows(
            "thinking_back",
            "id, chat_id, question, answer, found_answer, create_time, update_time",
            order,
        )

    def iter_conflicts(self) -> Iterator[dict]:
        yield from self._rows(
            "memory_conflicts",
            "id, conflict_content, answer, create_time, update_time, chat_id",
            "id ASC",
        )

    def iter_memory(self) -> Iterator[dict]:
        yield from self._rows(
            "memory",
            "id, memory_id, chat_id, memory_text, keywords, create_time, last_view_time",
            "id ASC",
        )

    def iter_hippo_topics(self) -> Iterator[Tuple[str, str, list]]:
        root = self.source_dir / "hippo_memorizer"
        if not root.is_dir():
            return
        for path in sorted(root.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                logger.warning("unreadable hippo_memorizer file %s: %s", path, exc)
                continue
            if not isinstance(data, dict):
                continue
            chat_id = _clean_text(data.get("chat_id")) or path.stem
            topics = data.get("topics")
            if not isinstance(topics, dict):
                continue
            for title, payload in topics.items():
                messages = []
                if isinstance(payload, dict):
                    messages = payload.get("messages") or []
                if not isinstance(messages, list) or not messages:
                    continue
                yield chat_id, str(title), [str(m) for m in messages]

    def fingerprint(self) -> Dict[str, Any]:
        try:
            stat = self.db_path.stat()
            size, mtime = stat.st_size, stat.st_mtime
        except OSError:
            size, mtime = 0, 0
        return {
            "path": str(self.db_path),
            "size": size,
            "mtime": mtime,
            "tables": sorted(self.tables),
            "streams": len(self.streams),
        }


# ── Memory writer ───────────────────────────────────────────────────────────


class _MemoryWriter:
    """Write one imported memory with ownership/idempotency checks."""

    def __init__(
        self,
        tree_store: Optional[TomlTreeStore],
        manifest: Dict[str, Any],
        options: ImportOptions,
        stats: ImportStats,
        now: float,
        source_epoch: float,
    ):
        self.tree_store = tree_store
        self.manifest = manifest
        self.options = options
        self.stats = stats
        self.now = now
        # Stable timestamp for source rows that have no timestamp of their
        # own (jargon/chest/hippo topics/most memory points). Using now would
        # change source_hash on every run and rewrite those memories forever;
        # the epoch is persisted in the manifest on the first real run.
        self.source_epoch = source_epoch
        self._seen: set = set()

    async def safe_store(self, **kwargs) -> str:
        """store() wrapper that records a per-item error and keeps going."""
        item_key = str(kwargs.get("item_key", "") or "memory")
        try:
            return await self.store(**kwargs)
        except Exception as exc:
            self.stats.error(item_key, exc)
            return "error"

    async def store(
        self,
        *,
        category: str,
        item_key: str,
        entity_id: str = "",
        entity_type: str = "",
        folder: str = "facts",
        base_dir: str = "",
        text: str,
        memory_type: str = "fact",
        importance: int = 5,
        tags: Optional[list] = None,
        source: Optional[dict] = None,
        timestamp: Optional[float] = None,
        semantic_id: str = "",
    ) -> str:
        text = _clean_text(text)
        if not text:
            self.stats.skip("empty")
            return "empty"
        if not semantic_id:
            semantic_id = f"mai_{category}_{_short_hash(text)}"
        tags = _merge_tags([IMPORT_TAG], tags or [])
        source_data = _toml_safe(source or {}) or {}
        if "time" not in source_data:
            source_data["time"] = _iso(timestamp or self.now)
        try:
            importance = max(1, min(10, int(importance)))
        except (TypeError, ValueError):
            importance = 5
        ts = _safe_float(timestamp, self.source_epoch) or self.source_epoch
        text_hash = _text_hash(text)
        dedupe_key = (entity_type, entity_id, base_dir, folder, text_hash)
        if dedupe_key in self._seen:
            self.stats.skip("duplicate")
            return "duplicate"
        self._seen.add(dedupe_key)
        source_hash = _payload_hash(
            {
                "text": text,
                "type": memory_type,
                "importance": importance,
                "tags": tags,
                "timestamp": ts,
            }
        )

        record = self.manifest["items"].get(item_key) or {}
        state = record.get("state") or ""
        if state == "deleted" and not self.options.force:
            self.stats.skip("deleted")
            return "deleted"

        known_ids = [str(x) for x in (record.get("memory_ids") or []) if x]
        memory_id = known_ids[0] if known_ids else semantic_id

        if self.options.dry_run:
            if (
                state in ("imported", "updated")
                and record.get("source_hash") == source_hash
                and record.get("text_hash") == text_hash
            ):
                self.stats.skip("unchanged")
                return "unchanged"
            if state in ("edited", "conflict"):
                self.stats.skip("edited")
                return "edited"
            if state in ("imported", "updated"):
                self.stats.add_updated(category)
                return "updated"
            self.stats.add_imported(category)
            return "added"

        if self.tree_store is None:
            raise RuntimeError("tree_store is required when dry_run is false")

        mem = await self.tree_store.get_memory(
            memory_id,
            entity_id=entity_id,
            entity_type=entity_type,
            folder=folder,
            base_dir=base_dir,
        )

        if mem is None:
            if state in ("imported", "updated", "edited", "conflict"):
                record.update({"state": "deleted", "deleted_at": self.now})
                self.manifest["items"][item_key] = record
                self.stats.skip("deleted")
                return "deleted"
            mem = await self.tree_store.add_memory(
                content_text=text,
                memory_type=memory_type,
                importance=importance,
                tags=tags,
                source=source_data,
                semantic_id=semantic_id,
                entity_id=entity_id,
                entity_type=entity_type,
                folder=folder,
                base_dir=base_dir,
            )
            mem.meta.update(
                timestamp=ts, last_accessed=self.now, access_count=0
            )
            if not await self.tree_store.update_memory(mem):
                raise RuntimeError(f"could not update imported memory {mem.id}")
            self.manifest["items"][item_key] = {
                "state": "imported",
                "source_hash": source_hash,
                "text_hash": text_hash,
                "memory_ids": [mem.id],
                "updated_at": self.now,
            }
            self.stats.add_imported(category)
            return "added"

        # A memory with the target id already exists.
        if IMPORT_TAG not in (mem.tags or []):
            self.manifest["items"][item_key] = {
                "state": "conflict",
                "source_hash": source_hash,
                "text_hash": text_hash,
                "memory_ids": [mem.id],
                "updated_at": self.now,
            }
            self.stats.skip("conflict")
            return "conflict"

        current_hash = _text_hash(mem.text or "")
        if state in ("edited", "conflict"):
            if record.get("source_hash") == source_hash:
                self.stats.skip("unchanged")
            else:
                self.stats.skip("edited")
            return "edited"

        if state in ("imported", "updated"):
            if (
                record.get("source_hash") == source_hash
                and record.get("text_hash") == text_hash
                and current_hash == record.get("text_hash")
            ):
                self.stats.skip("unchanged")
                return "unchanged"
            if current_hash != record.get("text_hash"):
                record.update({"state": "edited", "edited_at": self.now})
                self.manifest["items"][item_key] = record
                self.stats.skip("edited")
                return "edited"
        else:
            # No record yet (bird's-eye adoption after a crash, or a previous
            # importer run whose manifest was lost).
            if current_hash == text_hash:
                self.manifest["items"][item_key] = {
                    "state": "imported",
                    "source_hash": source_hash,
                    "text_hash": text_hash,
                    "memory_ids": [mem.id],
                    "updated_at": self.now,
                }
                self.stats.skip("unchanged")
                return "unchanged"
            self.manifest["items"][item_key] = {
                "state": "conflict",
                "source_hash": source_hash,
                "text_hash": text_hash,
                "memory_ids": [mem.id],
                "updated_at": self.now,
            }
            self.stats.skip("conflict")
            return "conflict"

        mem.text = text
        mem.type = memory_type
        mem.importance = importance
        mem.tags = tags
        mem.source = source_data
        mem.meta.update(timestamp=ts, last_accessed=self.now, access_count=0)
        if not await self.tree_store.update_memory(mem):
            raise RuntimeError(f"could not update memory {mem.id}")
        self.manifest["items"][item_key] = {
            "state": "updated",
            "source_hash": source_hash,
            "text_hash": text_hash,
            "memory_ids": [mem.id],
            "updated_at": self.now,
        }
        self.stats.add_updated(category)
        return "updated"


# ── Profile helpers ─────────────────────────────────────────────────────────


_PROFILE_TAGS = ("身份", "性格", "技能兴趣", "互动习惯", "关系", "其他")


def _profile_tag(category: str) -> str:
    value = category or ""
    if any(k in value for k in ("关系", "人际", "社交", "家庭")):
        return "关系"
    if any(k in value for k in ("性格", "个性", "情感", "情绪", "心理")):
        return "性格"
    if any(k in value for k in ("习惯", "作息", "行为", "互动", "偏好")):
        return "互动习惯"
    if any(k in value for k in ("身份", "个人", "姓名", "年龄", "性别", "籍贯", "职业", "学校", "住址")):
        return "身份"
    if any(
        k in value
        for k in (
            "兴趣", "爱好", "技能", "技术", "艺术", "游戏", "饮食",
            "消费", "设备", "学习", "创作", "音乐", "外貌", "角色",
        )
    ):
        return "技能兴趣"
    return "其他"


def _parse_memory_points(raw: Any) -> List[dict]:
    """Parse MaiBot memory_points JSON into category/content/weight dicts.

    The stored format is a JSON array of "分类:内容:权重" strings. Content may
    itself contain a colon, so category is split on the first colon and the
    weight is peeled off the end.
    """
    points = _json_list(raw)
    out: List[dict] = []
    for index, item in enumerate(points):
        if not isinstance(item, str):
            continue
        value = item.strip()
        if not value:
            continue
        category = ""
        rest = value
        if ":" in value:
            category, rest = value.split(":", 1)
            category = category.strip()
        weight = 1.0
        match = re.match(r"^(.*):([0-9]+(?:\.[0-9]+)?)\s*$", rest, re.S)
        if match:
            rest = match.group(1)
            try:
                weight = float(match.group(2))
            except ValueError:
                weight = 1.0
        content = rest.strip()
        if not content:
            continue
        out.append(
            {
                "index": index,
                "category": category or "其他",
                "content": content,
                "weight": weight,
            }
        )
    return out


def _point_importance(weight: float) -> int:
    if weight >= 10:
        return 8
    if weight >= 5:
        return 7
    if weight >= 3:
        return 6
    if weight >= 2:
        return 5
    return 4


def _build_user_profile(
    source: QanMoCoreSource, row: dict, options: ImportOptions, now: float
) -> Tuple[dict, List[dict]]:
    user_id = _clean_text(row.get("user_id"))
    nickname = _clean_text(row.get("nickname"))
    mapped_name = source.name_map["u"].get(user_id, "")
    name = (
        _clean_text(row.get("person_name"))
        or nickname
        or mapped_name
        or user_id
    )
    groups: Dict[str, str] = {}
    for item in _json_list(row.get("group_nick_name")):
        if isinstance(item, dict):
            group_id = _clean_text(item.get("group_id"))
            group_nick = _clean_text(item.get("group_nick_name"))
            if group_id:
                groups[group_id] = group_nick
    aliases: List[str] = []
    for group_nick in groups.values():
        if group_nick and group_nick not in aliases and group_nick not in (name, nickname):
            aliases.append(group_nick)
    if mapped_name and mapped_name not in aliases and mapped_name not in (name, nickname):
        aliases.append(mapped_name)

    points = _parse_memory_points(row.get("memory_points"))
    seen = set()
    unique_points = []
    for point in points:
        h = _text_hash(point["content"])
        if h in seen:
            continue
        seen.add(h)
        unique_points.append(point)

    profile_facts: List[str] = []
    total_chars = 0
    for point in sorted(unique_points, key=lambda p: (-p["weight"], p["index"])):
        if len(profile_facts) >= options.profile_facts_limit:
            break
        content = point["content"]
        if len(content) > options.profile_fact_clip:
            content = content[: options.profile_fact_clip - 1] + "…"
        line = f"[{_profile_tag(point['category'])}] {content}"
        if profile_facts and total_chars + len(line) > options.profile_facts_max_chars:
            break
        profile_facts.append(line)
        total_chars += len(line) + 1

    last_interaction = _safe_float(row.get("last_know"), 0.0) or _safe_float(
        row.get("know_since"), 0.0
    )
    data = {
        "name": name,
        "nickname": nickname,
        "description": "",
        "platform": _clean_text(row.get("platform")),
        "traits": [],
        "preferences": {},
        "relationships": {},
        "facts": profile_facts,
        "aliases": aliases,
        "interaction_count": _safe_int(row.get("know_times"), 0),
        "last_interaction": last_interaction,
        "metadata": {
            "person_id": _clean_text(row.get("person_id")),
            "name_reason": _clean_text(row.get("name_reason")),
            "groups": groups,
            "know_times": _safe_float(row.get("know_times"), 0.0),
            "know_since": _safe_float(row.get("know_since"), 0.0),
            "last_know": _safe_float(row.get("last_know"), 0.0),
        },
    }
    return data, unique_points


def _build_group_profile(
    source: QanMoCoreSource, stream: dict, now: float
) -> dict:
    group_id = _clean_text(stream.get("group_id"))
    mapped = source.name_map["g"].get(group_id, "")
    raw_name = _clean_text(stream.get("group_name"))
    name = raw_name or mapped or group_id
    aliases: List[str] = []
    for candidate in (raw_name, mapped):
        if candidate and candidate not in aliases and candidate != name:
            aliases.append(candidate)
    stream_id = _clean_text(stream.get("stream_id"))
    return {
        "name": name,
        "nickname": "",
        "description": "",
        "platform": _clean_text(stream.get("platform")),
        "traits": [],
        "preferences": {},
        "relationships": {},
        "facts": [],
        "aliases": aliases,
        "interaction_count": int(
            source.history_counts.get(stream_id, 0) + source.qa_counts.get(stream_id, 0)
        ),
        "last_interaction": _safe_float(stream.get("last_active_time"), 0.0),
        "metadata": {
            "stream_id": stream_id,
            "group_id": group_id,
            "group_name": raw_name,
        },
    }


def _build_stream_user_profile(source: QanMoCoreSource, stream: dict, now: float) -> dict:
    user_id = _clean_text(stream.get("user_id"))
    mapped = source.name_map["u"].get(user_id, "")
    nickname = _clean_text(stream.get("user_nickname"))
    name = mapped or nickname or user_id
    aliases: List[str] = []
    for candidate in (nickname, mapped):
        if candidate and candidate not in aliases and candidate != name:
            aliases.append(candidate)
    return {
        "name": name,
        "nickname": nickname,
        "description": "",
        "platform": _clean_text(stream.get("platform")),
        "traits": [],
        "preferences": {},
        "relationships": {},
        "facts": [],
        "aliases": aliases,
        "interaction_count": 0,
        "last_interaction": _safe_float(stream.get("last_active_time"), 0.0),
        "metadata": {
            "stream_id": _clean_text(stream.get("stream_id")),
            "user_id": user_id,
            "user_nickname": nickname,
        },
    }


def _merge_entity_profile(profile: EntityProfile, data: dict, options: ImportOptions) -> bool:
    changed = False
    for field_name in ("name", "nickname", "description", "platform"):
        value = _clean_text(data.get(field_name))
        if value and not getattr(profile, field_name, ""):
            setattr(profile, field_name, value)
            changed = True
    for field_name in ("traits", "aliases"):
        current = list(getattr(profile, field_name, []) or [])
        for value in data.get(field_name) or []:
            value = _clean_text(value)
            if value and value not in current:
                current.append(value)
                changed = True
        setattr(profile, field_name, current)
    native_facts = list(profile.facts or [])
    merged_facts = list(native_facts)
    for value in data.get("facts") or []:
        value = _clean_text(value)
        if value and value not in merged_facts:
            merged_facts.append(value)
            changed = True
    cap = max(options.profile_facts_limit, len(native_facts))
    if len(merged_facts) > cap:
        merged_facts = merged_facts[:cap]
    profile.facts = merged_facts
    for field_name in ("preferences", "relationships"):
        current = getattr(profile, field_name, {}) or {}
        if not isinstance(current, dict):
            current = {}
        incoming = data.get(field_name) or {}
        if isinstance(incoming, dict):
            for key, value in incoming.items():
                if key not in current:
                    current[key] = value
                    changed = True
            setattr(profile, field_name, current)
    metadata = profile.metadata if isinstance(profile.metadata, dict) else {}
    qan = data.get("metadata") or {}
    if not isinstance(qan, dict):
        qan = {}
    if metadata.get("qanmocore") != qan:
        metadata["qanmocore"] = qan
        profile.metadata = metadata
        changed = True
    interaction_count = _safe_int(data.get("interaction_count"), 0)
    if interaction_count > (profile.interaction_count or 0):
        profile.interaction_count = interaction_count
        changed = True
    last_interaction = _safe_float(data.get("last_interaction"), 0.0)
    if last_interaction > (profile.last_interaction or 0.0):
        profile.last_interaction = last_interaction
        changed = True
    return changed


async def _merge_profile(
    *,
    profile_store: Optional[EntityProfileStore],
    entity_id: str,
    entity_type: str,
    item_key: str,
    data: dict,
    options: ImportOptions,
    stats: ImportStats,
    manifest: Dict[str, Any],
    now: float,
) -> None:
    fpath = Path(get_entity_profile_path(entity_id, entity_type))
    exists = fpath.is_file()

    # Never merge into a profile we cannot parse: saving the in-memory default
    # that get_profile() returns would silently wipe the user's file.
    if exists:
        try:
            raw = json.loads(fpath.read_text(encoding="utf-8"))
            EntityProfile.from_dict(raw)
        except Exception as exc:
            raise RuntimeError(f"cannot read existing profile {fpath}: {exc}") from exc

    record = manifest["profiles"].get(item_key) or {}
    source_hash = _payload_hash(data)

    if options.dry_run:
        if exists and record.get("hash") == source_hash:
            stats.skip("profile_unchanged")
        elif record.get("state") == "deleted" and not exists:
            stats.skip("profile_deleted")
        else:
            stats.profiles_written += 1
        return

    if profile_store is None:
        raise RuntimeError("profile_store is required when dry_run is false")
    if record.get("state") == "deleted" and not exists and not options.force:
        stats.skip("profile_deleted")
        return
    if exists and record.get("hash") == source_hash:
        stats.skip("profile_unchanged")
        return
    if not exists and record.get("state") in ("imported", "updated") and not options.force:
        record.update({"state": "deleted", "deleted_at": now})
        manifest["profiles"][item_key] = record
        stats.skip("profile_deleted")
        return

    profile = await profile_store.get_profile(entity_id, entity_type)
    changed = _merge_entity_profile(profile, data, options)
    if not await profile_store.save_profile(profile):
        raise RuntimeError(f"could not save profile {entity_id} ({entity_type})")
    manifest["profiles"][item_key] = {
        "hash": source_hash,
        "state": "imported",
        "updated_at": now,
    }
    stats.profiles_written += 1
    if not changed:
        stats.skip("profile_noop")


async def _import_profiles(
    source: QanMoCoreSource,
    options: ImportOptions,
    profile_store: Optional[EntityProfileStore],
    stats: ImportStats,
    manifest: Dict[str, Any],
    now: float,
) -> None:
    person_ids: set = set()
    for row in source.iter_person_info():
        user_id = _clean_text(row.get("user_id"))
        if not user_id:
            stats.skip("empty_user_id")
            continue
        person_ids.add(user_id)
        entity_id = f"{options.adapter}:{user_id}"
        data, points = _build_user_profile(source, row, options, now)
        item_key = f"person:{row.get('id')}"
        try:
            await _merge_profile(
                profile_store=profile_store,
                entity_id=entity_id,
                entity_type=ENTITY_USER,
                item_key=item_key,
                data=data,
                options=options,
                stats=stats,
                manifest=manifest,
                now=now,
            )
        except Exception as exc:
            stats.error(item_key, exc)
            continue
        if CATEGORY_PROFILES not in options.include:
            continue
        # Memory points are stored as entity facts independently of the
        # top-N facts copied into the profile.
        last_know = _safe_float(row.get("last_know"), 0.0) or _safe_float(
            row.get("know_since"), 0.0
        )
        for point in points:
            item_key_point = (
                f"point:{row.get('id')}:{_short_hash(point['content'])}"
            )
            tags = [IMPORT_TAG, "memory-point", point["category"][:40]]
            source_dict = {
                "origin": "qanmocore",
                "table": "person_info",
                "person_id": _clean_text(row.get("person_id")),
                "user_id": user_id,
                "category": point["category"],
                "weight": point["weight"],
            }
            try:
                await _writer.store(
                    category="points",
                    item_key=item_key_point,
                    entity_id=entity_id,
                    entity_type=ENTITY_USER,
                    folder="facts",
                    text=point["content"],
                    memory_type="fact",
                    importance=_point_importance(point["weight"]),
                    tags=tags,
                    source=source_dict,
                    timestamp=last_know or None,
                    semantic_id=f"mai_point_{row.get('id')}_{_short_hash(point['content'])}",
                )
            except Exception as exc:
                stats.error(item_key_point, exc)

    for stream in source.group_streams():
        group_id = _clean_text(stream.get("group_id"))
        entity_id = f"{options.adapter}:{group_id}"
        stream_id = _clean_text(stream.get("stream_id"))
        data = _build_group_profile(source, stream, now)
        item_key = f"group_profile:{stream_id}"
        try:
            await _merge_profile(
                profile_store=profile_store,
                entity_id=entity_id,
                entity_type=ENTITY_GROUP,
                item_key=item_key,
                data=data,
                options=options,
                stats=stats,
                manifest=manifest,
                now=now,
            )
        except Exception as exc:
            stats.error(item_key, exc)

    for stream in source.private_streams():
        user_id = _clean_text(stream.get("user_id"))
        if not user_id or user_id in person_ids:
            continue
        entity_id = f"{options.adapter}:{user_id}"
        stream_id = _clean_text(stream.get("stream_id"))
        data = _build_stream_user_profile(source, stream, now)
        item_key = f"stream_user:{stream_id}"
        try:
            await _merge_profile(
                profile_store=profile_store,
                entity_id=entity_id,
                entity_type=ENTITY_USER,
                item_key=item_key,
                data=data,
                options=options,
                stats=stats,
                manifest=manifest,
                now=now,
            )
        except Exception as exc:
            stats.error(item_key, exc)


# Module-level writer handle used by category import helpers. Keeping it here
# lets each helper stay a free function (easier to test) without threading the
# writer through every signature.
_writer: Optional[_MemoryWriter] = None


def _time_in_range(value: float, options: ImportOptions) -> bool:
    if options.since is not None and value and value < options.since:
        return False
    if options.until is not None and value and value > options.until:
        return False
    return True


def _tags_with_keywords(base: List[str], keywords: list, limit: int = 10) -> List[str]:
    tags = list(base)
    for keyword in keywords[:limit]:
        keyword = _clean_text(keyword)
        if keyword and keyword not in tags:
            tags.append(keyword[:40])
    return tags


async def _import_history(
    source: QanMoCoreSource,
    writer: _MemoryWriter,
    options: ImportOptions,
    stats: ImportStats,
) -> None:
    per_chat: Dict[str, int] = {}
    for row in source.iter_chat_history(newest_first=bool(options.max_per_chat)):
        end_time = _safe_float(row.get("end_time"), 0.0)
        if not _time_in_range(end_time, options):
            stats.skip("time_filter")
            continue
        chat_id = _clean_text(row.get("chat_id"))
        if options.max_per_chat is not None:
            used = per_chat.get(chat_id, 0)
            if used >= options.max_per_chat:
                stats.skip("max_per_chat")
                continue
            per_chat[chat_id] = used + 1
        mapped = source.entity_for_stream(chat_id)
        if mapped is None:
            stats.skip("missing_stream")
            continue
        summary = _clean_text(row.get("summary"))
        if not summary:
            stats.skip("empty")
            continue
        theme = _clean_text(row.get("theme"))
        text = f"{theme}\n{summary}" if theme and theme != summary else summary
        entity_id, entity_type, session = mapped
        keywords = _json_list(row.get("keywords"))
        key_points = _json_list(row.get("key_point"))
        tags = _tags_with_keywords([IMPORT_TAG, "chat-history"], keywords)
        source_dict = {
            "origin": "qanmocore",
            "table": "chat_history",
            "id": row.get("id"),
            "session": session,
            "time": _iso(end_time),
            "participants": _json_list(row.get("participants")),
            "keywords": keywords,
            "key_points": key_points,
            "count": _safe_int(row.get("count"), 0),
            "forget_times": _safe_int(row.get("forget_times"), 0),
        }
        await writer.safe_store(
            category="history",
            item_key=f"history:{row.get('id')}",
            entity_id=entity_id,
            entity_type=entity_type,
            folder="reflections",
            text=text,
            memory_type="reflection",
            importance=5,
            tags=tags,
            source=source_dict,
            timestamp=end_time,
            semantic_id=f"mai_history_{row.get('id')}",
        )


async def _import_chest(
    source: QanMoCoreSource,
    writer: _MemoryWriter,
    options: ImportOptions,
    stats: ImportStats,
) -> None:
    for row in source.iter_chest():
        mapped = source.entity_for_stream(_clean_text(row.get("chat_id")))
        if mapped is None:
            stats.skip("missing_stream")
            continue
        title = _clean_text(row.get("title"))
        content = _clean_text(row.get("content"))
        text = f"{title}\n{content}" if title and content else (title or content)
        if not text:
            stats.skip("empty")
            continue
        entity_id, entity_type, session = mapped
        await writer.safe_store(
            category="chest",
            item_key=f"chest:{row.get('id')}",
            entity_id=entity_id,
            entity_type=entity_type,
            folder="facts",
            text=text,
            memory_type="fact",
            importance=7,
            tags=[IMPORT_TAG, "memory-chest", "knowledge"],
            source={
                "origin": "qanmocore",
                "table": "memory_chest",
                "id": row.get("id"),
                "session": session,
                "locked": _safe_int(row.get("locked"), 0),
            },
            timestamp=None,
            semantic_id=f"mai_chest_{row.get('id')}",
        )


def _parse_jargon_streams(raw: Any) -> List[str]:
    text = _clean_text(raw)
    if not text:
        return []
    if text.startswith("["):
        try:
            data = json.loads(text)
        except ValueError:
            data = None
        if isinstance(data, list):
            out: List[str] = []
            for item in data:
                if isinstance(item, (list, tuple)) and item:
                    out.append(_clean_text(item[0]))
                elif isinstance(item, str):
                    out.append(_clean_text(item))
            return [x for x in out if x]
    return [text]


async def _import_jargon(
    source: QanMoCoreSource,
    writer: _MemoryWriter,
    options: ImportOptions,
    stats: ImportStats,
) -> None:
    for row in source.iter_jargon():
        meaning = _clean_text(row.get("meaning"))
        content = _clean_text(row.get("content"))
        if not content:
            stats.skip("empty")
            continue
        if not meaning:
            stats.skip("jargon_no_meaning")
            continue
        text = f"「{content}」：{meaning}"
        stream_ids = _parse_jargon_streams(row.get("chat_id"))
        await writer.safe_store(
            category="jargon",
            item_key=f"jargon:{row.get('id')}",
            entity_id="",
            entity_type="",
            folder="facts",
            base_dir="global",
            text=text,
            memory_type="fact",
            importance=6,
            tags=[IMPORT_TAG, "jargon", "黑话"],
            source={
                "origin": "qanmocore",
                "table": "jargon",
                "id": row.get("id"),
                "content": content,
                "is_global": _safe_int(row.get("is_global"), 0),
                "count": _safe_int(row.get("count"), 0),
                "is_complete": _safe_int(row.get("is_complete"), 0),
                "stream_ids": stream_ids,
            },
            timestamp=None,
            semantic_id=f"mai_jargon_{row.get('id')}",
        )


def _is_negative_answer(answer: str) -> bool:
    stripped = answer.strip()
    return any(stripped.startswith(prefix) for prefix in _NEGATIVE_QA_PREFIXES)


async def _import_qa(
    source: QanMoCoreSource,
    writer: _MemoryWriter,
    options: ImportOptions,
    stats: ImportStats,
) -> None:
    per_chat: Dict[str, int] = {}
    for row in source.iter_qa(newest_first=bool(options.max_per_chat)):
        created = _safe_float(row.get("create_time"), 0.0)
        if not _time_in_range(created, options):
            stats.skip("time_filter")
            continue
        chat_id = _clean_text(row.get("chat_id"))
        if options.max_per_chat is not None:
            used = per_chat.get(chat_id, 0)
            if used >= options.max_per_chat:
                stats.skip("max_per_chat")
                continue
            per_chat[chat_id] = used + 1
        answer = _clean_text(row.get("answer"))
        question = _clean_text(row.get("question"))
        if not answer or not question or _safe_int(row.get("found_answer"), 0) != 1:
            stats.skip("qa_no_answer")
            continue
        if not options.keep_unanswered and _is_negative_answer(answer):
            stats.skip("qa_unanswered")
            continue
        mapped = source.entity_for_stream(chat_id)
        if mapped is None:
            stats.skip("missing_stream")
            continue
        entity_id, entity_type, session = mapped
        await writer.safe_store(
            category="qa",
            item_key=f"qa:{row.get('id')}",
            entity_id=entity_id,
            entity_type=entity_type,
            folder="facts",
            text=f"Q：{question}\nA：{answer}",
            memory_type="fact",
            importance=5,
            tags=[IMPORT_TAG, "qa", "thinking-back"],
            source={
                "origin": "qanmocore",
                "table": "thinking_back",
                "id": row.get("id"),
                "session": session,
                "question": question,
                "found_answer": _safe_int(row.get("found_answer"), 0),
            },
            timestamp=created,
            semantic_id=f"mai_qa_{row.get('id')}",
        )


async def _import_conflicts(
    source: QanMoCoreSource,
    writer: _MemoryWriter,
    options: ImportOptions,
    stats: ImportStats,
) -> None:
    for row in source.iter_conflicts():
        answer = _clean_text(row.get("answer"))
        question = _clean_text(row.get("conflict_content"))
        if not answer or not question:
            stats.skip("conflict_no_answer")
            continue
        created = _safe_float(row.get("create_time"), 0.0)
        if not _time_in_range(created, options):
            stats.skip("time_filter")
            continue
        mapped = source.entity_for_stream(_clean_text(row.get("chat_id")))
        if mapped is None:
            stats.skip("missing_stream")
            continue
        entity_id, entity_type, session = mapped
        await writer.safe_store(
            category="conflicts",
            item_key=f"conflict:{row.get('id')}",
            entity_id=entity_id,
            entity_type=entity_type,
            folder="facts",
            text=f"{question}\n答：{answer}",
            memory_type="fact",
            importance=6,
            tags=[IMPORT_TAG, "conflict", "resolution"],
            source={
                "origin": "qanmocore",
                "table": "memory_conflicts",
                "id": row.get("id"),
                "session": session,
            },
            timestamp=created,
            semantic_id=f"mai_conflict_{row.get('id')}",
        )


async def _import_memory_table(
    source: QanMoCoreSource,
    writer: _MemoryWriter,
    options: ImportOptions,
    stats: ImportStats,
) -> None:
    for row in source.iter_memory():
        text = _clean_text(row.get("memory_text"))
        if not text:
            stats.skip("empty")
            continue
        mapped = source.entity_for_stream(_clean_text(row.get("chat_id")))
        if mapped is None:
            stats.skip("missing_stream")
            continue
        created = _safe_float(row.get("create_time"), 0.0)
        if not _time_in_range(created, options):
            stats.skip("time_filter")
            continue
        entity_id, entity_type, session = mapped
        keywords = _json_list(row.get("keywords"))
        await writer.safe_store(
            category="memory",
            item_key=f"memory:{row.get('id')}",
            entity_id=entity_id,
            entity_type=entity_type,
            folder="facts",
            text=text,
            memory_type="fact",
            importance=5,
            tags=_tags_with_keywords([IMPORT_TAG, "legacy-memory"], keywords),
            source={
                "origin": "qanmocore",
                "table": "memory",
                "id": row.get("id"),
                "memory_id": row.get("memory_id"),
                "session": session,
                "keywords": keywords,
            },
            timestamp=created,
            semantic_id=f"mai_memory_{row.get('id')}",
        )


async def _import_hippo_topics(
    source: QanMoCoreSource,
    writer: _MemoryWriter,
    options: ImportOptions,
    stats: ImportStats,
) -> None:
    for chat_id, title, messages in source.iter_hippo_topics():
        mapped = source.entity_for_stream(chat_id)
        if mapped is None:
            stats.skip("missing_stream")
            continue
        head = messages[:3]
        tail = messages[-3:] if len(messages) > 3 else []
        excerpt = "\n".join(head + (["…"] if tail else []) + tail)
        if len(excerpt) > 800:
            excerpt = excerpt[:800] + "…"
        text = f"{title}\n{excerpt}" if title else excerpt
        entity_id, entity_type, session = mapped
        topic_key = _short_hash(f"{chat_id}:{title}")
        await writer.safe_store(
            category="hippo-topics",
            item_key=f"hippo:{topic_key}",
            entity_id=entity_id,
            entity_type=entity_type,
            folder="facts",
            text=text,
            memory_type="fact",
            importance=4,
            tags=[IMPORT_TAG, "hippo-topic"],
            source={
                "origin": "qanmocore",
                "table": "hippo_memorizer",
                "chat_id": chat_id,
                "title": title,
                "session": session,
                "messages": len(messages),
            },
            timestamp=None,
            semantic_id=f"mai_topic_{topic_key}",
        )


# ── Entry point ─────────────────────────────────────────────────────────────


async def import_qanmocore(
    source_dir,
    *,
    tree_store: Optional[TomlTreeStore] = None,
    profile_store: Optional[EntityProfileStore] = None,
    options: Optional[ImportOptions] = None,
    allow_live_source: bool = False,
) -> ImportStats:
    """Import MaiBot/QanMoCore memory into the current KiraOS data root.

    "tree_store" and "profile_store" are only required when "options.dry_run"
    is false. The caller is responsible for calling set_data_root() before
    invoking this function.
    """
    global _writer
    options = options or ImportOptions()
    now = _safe_float(options.now, time.time()) or time.time()
    stats = ImportStats(dry_run=options.dry_run)
    stats.output_root = get_memory_root()
    manifest_path = (
        Path(options.manifest_path)
        if options.manifest_path
        else Path(get_memory_root()) / MANIFEST_NAME
    )
    stats.manifest_path = str(manifest_path)

    if not options.dry_run:
        if tree_store is None or profile_store is None:
            raise ValueError("tree_store and profile_store are required for a real import")
        ensure_directory_structure()

    manifest = load_manifest(manifest_path)
    # A stable epoch for timestamp-less source rows. Persisted on the first
    # real run so reruns compute the same source_hash and stay no-ops.
    source_epoch = _safe_float(manifest.get("imported_at"), 0.0)
    if source_epoch <= 0:
        source_epoch = now
        manifest["imported_at"] = now
    writer = _MemoryWriter(
        tree_store, manifest, options, stats, now, source_epoch
    )
    _writer = writer
    started = time.time()

    try:
        with QanMoCoreSource(
            Path(source_dir), adapter=options.adapter, allow_live=allow_live_source
        ) as source:
            stats.source = source.fingerprint()
            logger.info(
                "QanMoCore import starting: source=%s adapter=%s include=%s dry_run=%s",
                source_dir,
                options.adapter,
                ",".join(sorted(options.include)),
                options.dry_run,
            )
            if CATEGORY_PROFILES in options.include:
                await _import_profiles(
                    source, options, profile_store, stats, manifest, now
                )

            category_handlers = (
                (CATEGORY_HISTORY, _import_history),
                (CATEGORY_CHEST, _import_chest),
                (CATEGORY_JARGON, _import_jargon),
                (CATEGORY_QA, _import_qa),
                (CATEGORY_CONFLICTS, _import_conflicts),
                (CATEGORY_MEMORY, _import_memory_table),
            )
            for category, handler in category_handlers:
                if category not in options.include:
                    continue
                try:
                    await handler(source, writer, options, stats)
                except Exception as exc:
                    stats.error(category, exc)

            if CATEGORY_HIPPO in options.include:
                try:
                    await _import_hippo_topics(source, writer, options, stats)
                except Exception as exc:
                    stats.error(CATEGORY_HIPPO, exc)

            if not options.dry_run:
                manifest["version"] = MANIFEST_VERSION
                manifest["source"] = stats.source
                manifest["run_at"] = _iso(now)
                manifest["options"] = {
                    "adapter": options.adapter,
                    "include": sorted(options.include),
                    "profile_facts_limit": options.profile_facts_limit,
                    "since": options.since,
                    "until": options.until,
                    "max_per_chat": options.max_per_chat,
                    "keep_unanswered": options.keep_unanswered,
                }
                try:
                    write_manifest(manifest_path, manifest)
                except Exception as exc:
                    stats.error("manifest", exc)
    finally:
        _writer = None
        stats.elapsed = time.time() - started

    logger.info(
        "QanMoCore import finished: imported=%s updated=%s profiles=%d skipped=%s errors=%d elapsed=%.1fs",
        stats.imported,
        stats.updated,
        stats.profiles_written,
        stats.skipped,
        len(stats.errors),
        stats.elapsed,
    )
    return stats
