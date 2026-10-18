"""Reconcile kira_plugin_hippocampus_memory data into unified KiraOS storage.

Runs on every launch; idempotent and incremental.

- Source (`data/plugin_data/kira_plugin_hippocampus_memory/memory/`) is read-only; its
  SQLite index is opened `mode=ro`.
- Hippocampus and KiraOS share the same on-disk layout, so each source file's namespace
  is derived with KiraOS's own `MemoryIndex._parse_path` — the rule the startup index
  rebuild applies. Covers entities/*/{facts,reflections,skills}, global/facts,
  global/self/*, global/skills/* and legacy JSON memories.
- Runtime meta (timestamp / last_accessed / access_count) comes from the hippocampus
  index (by namespace, or by id + content hash when the data root moved), the file's
  embedded meta and `source.time`; merged as earliest timestamp, largest counts.
- An existing KiraOS copy is matched by id + content hash, the `{id}_{hash8}` collision
  suffix, content hash within the namespace, or a `hippocampus-import` copy edited
  later. Data from the 4.0.x one-shot migration is repaired in place (lost meta
  restored, dropped memories added), not duplicated.
- The manifest (`data/memory/.hippocampus_migrated`, v2) records every processed file.
  A processed memory that has disappeared from KiraOS was deleted or decayed there and
  is never resurrected; neither is anything archived in KiraOS or belonging to an
  entity deleted from the WebUI.
- The hippocampus plugin is disabled once, after the first successful run. If nothing
  could be imported, both plugins stay enabled and no manifest is written.
"""

import asyncio
import hashlib
import json
import os
import shutil
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

try:
    import tomllib  # Python 3.11+
except ImportError:
    import tomli as tomllib  # Python 3.10 fallback

from core.logging_manager import get_logger
from core.utils.path_utils import get_data_path

from .memory_paths import (
    get_entity_profile_path,
    get_archive_dir,
    _path_segment_to_id,
    _id_to_path_segment,
    _validate_id,
    ENTITY_USER,
    ENTITY_GROUP,
    ENTITY_CHANNEL,
)
from .entity_profile import EntityProfileStore
from .memory_index import MemoryIndex, normalize_tags
from .toml_tree_store import TomlTreeStore

logger = get_logger("kiraos_hippocampus_migration", "cyan")

_MARKER_NAME = ".hippocampus_migrated"
_HIPPOCAMPUS_PLUGIN_ID = "kira_plugin_hippocampus_memory"
_MANIFEST_VERSION = 2
_IMPORT_TAG = "hippocampus-import"
_ENTITY_FOLDERS = ("facts", "reflections", "skills")

# Per-file record states. OK = KiraOS owns this memory now. TERMINAL = tombstone,
# never imported again. The rest are re-checked whenever the source file changes.
_ST_IMPORTED = "imported"
_ST_MERGED = "merged"
_ST_ARCHIVED = "archived"
_ST_DELETED = "deleted"
_ST_ENTITY_DELETED = "entity_deleted"
_ST_INVALID = "invalid"
_OK_STATES = {_ST_IMPORTED, _ST_MERGED, _ST_ARCHIVED, _ST_DELETED}
_TERMINAL_STATES = {_ST_ARCHIVED, _ST_DELETED, _ST_ENTITY_DELETED}

_UP_TO_DATE = "up_to_date"
_SKIPPED = "skipped"

_STAT_KEYS = ("entities", "facts", "reflections", "global_facts", "global_self", "skills")

# (entity_type, entity_id, folder, base_dir)
Namespace = Tuple[str, str, str, str]


async def migrate_hippocampus_if_needed(
    tree_store, profile_store: EntityProfileStore, ctx
) -> dict:
    """Reconcile hippocampus data into KiraOS if present.

    Returns:
        {"migrated": bool, "failed": bool (only on total failure),
         "entities", "facts", "reflections", "global_facts", "global_self", "skills":
             newly imported this run,
         "merged": existing KiraOS copies updated, "up_to_date": unchanged,
         "skipped": archived / deleted / unreadable, "errors": retried next launch}
    """
    stats = dict.fromkeys(_STAT_KEYS, 0)
    counts = {"merged": 0, _UP_TO_DATE: 0, _SKIPPED: 0, "errors": 0}

    marker_path = get_data_path() / "memory" / _MARKER_NAME
    manifest = _load_manifest(marker_path)

    hippo_root = get_data_path() / "plugin_data" / _HIPPOCAMPUS_PLUGIN_ID / "memory"
    if not hippo_root.is_dir():
        if manifest is None:
            _write_manifest(marker_path, _new_manifest(plugin_disabled=False))
        return {"migrated": False, **stats, **counts}

    logger.info(f"Hippocampus data detected at {hippo_root}, reconciling...")
    if manifest is None:
        manifest = _new_manifest(plugin_disabled=False)
    processed: Dict[str, dict] = manifest["processed"]
    profiles: Dict[str, dict] = manifest["profiles"]

    hippo_meta = _HippoMeta(hippo_root)
    deleted_entities = _load_deleted_entities(manifest["since"])

    # === 1. Entity profiles ===
    for profile_json, entity_type, entity_id in _iter_profiles(hippo_root):
        rel = profile_json.relative_to(hippo_root).as_posix()
        try:
            if await _reconcile_profile(
                profile_json, rel, entity_type, entity_id,
                profiles, profile_store, deleted_entities,
            ):
                stats["entities"] += 1
        except Exception as e:
            counts["errors"] += 1
            logger.warning(f"Failed to merge hippocampus profile {rel}: {e}")

    # === 2. Memories (entities + global) ===
    total_memories = 0
    for path in _iter_memory_files(hippo_root):
        ns = _namespace_for(path, hippo_root)
        if ns is None:
            logger.warning(f"Skipping hippocampus file outside known layout: {path}")
            continue
        total_memories += 1
        rel = path.relative_to(hippo_root).as_posix()
        try:
            outcome, record = await _reconcile_memory(
                path, rel, ns, processed.get(rel), tree_store, hippo_meta, deleted_entities
            )
        except Exception as e:
            # Not recorded: retried on the next launch.
            counts["errors"] += 1
            logger.warning(f"Failed to reconcile hippocampus memory {rel}: {e}")
            continue
        processed[rel] = record
        if outcome == _ST_IMPORTED:
            stats[_bucket(ns)] += 1
        elif outcome == _ST_MERGED:
            counts["merged"] += 1
        else:
            counts[outcome] += 1

    # === 3. Hippocampus archive → KiraOS archive/hippocampus/ (never overwrites) ===
    _copy_archive(hippo_root)

    # === 4. Failure gate ===
    # Re-runs report already-reconciled memories as up_to_date, not as failures.
    # Only "source has memories, nothing was ever reconciled, and this run hit
    # errors" means the data did not land — then keep the old plugin enabled
    # and write no manifest, so the next launch retries from scratch.
    owned = sum(1 for r in processed.values() if r.get("state") in _OK_STATES)
    owned_profiles = sum(1 for r in profiles.values() if r.get("state") != _ST_INVALID)
    landed = owned if total_memories else owned_profiles
    if counts["errors"] and landed == 0:
        logger.error(
            f"Hippocampus reconcile could not import any of {total_memories} source "
            "memories (or profiles) — leaving the hippocampus plugin ENABLED and NOT writing the "
            "manifest; will retry next launch. See warnings above."
        )
        return {"migrated": False, "failed": True, **stats, **counts}

    if counts["errors"]:
        logger.warning(
            f"{counts['errors']} hippocampus item(s) failed to reconcile and will be "
            "retried next launch (see warnings above)."
        )

    # === 5. Disable the old plugin once; respect a later manual re-enable ===
    if not manifest["plugin_disabled"]:
        manifest["plugin_disabled"] = await _disable_hippocampus_plugin(ctx)
    if manifest["since"] is None and (owned or owned_profiles):
        manifest["since"] = time.time()

    _write_manifest(marker_path, manifest)

    imported = sum(stats[k] for k in _STAT_KEYS if k != "entities")
    if imported or counts["merged"] or stats["entities"]:
        logger.info(
            f"Hippocampus reconcile: imported {stats['facts']} facts, "
            f"{stats['reflections']} reflections, {stats['global_facts']} global facts, "
            f"{stats['global_self']} global/self, {stats['skills']} skills; "
            f"merged {stats['entities']} profiles; updated meta of {counts['merged']} "
            f"existing memories; {counts[_UP_TO_DATE]} up to date, "
            f"{counts[_SKIPPED]} skipped (archived/deleted/unreadable)"
        )
    else:
        logger.debug("Hippocampus reconcile: nothing new")
    return {"migrated": True, **stats, **counts}


# ── Manifest ─────────────────────────────────────────────────────────────────


def _new_manifest(*, plugin_disabled: bool, since: Optional[float] = None) -> dict:
    return {
        "version": _MANIFEST_VERSION,
        # First time hippocampus data landed in KiraOS. Entities deleted from the
        # KiraOS WebUI after this point must not be re-imported.
        "since": since,
        "plugin_disabled": plugin_disabled,
        "processed": {},  # rel path → per-memory record
        "profiles": {},  # rel path → per-profile record
    }


def _load_manifest(marker_path: Path) -> Optional[dict]:
    """Load the v2 manifest, upgrading markers written by 4.0.x.

    4.0.x wrote either an empty touch file (no hippocampus data at that launch)
    or a JSON stats dict (a one-shot migration ran and disabled the plugin).
    Neither recorded which files were imported, so every source file is
    reconciled again — matching against what is already in KiraOS rather than
    re-adding it.
    """
    if not marker_path.exists():
        return None
    try:
        content = marker_path.read_text(encoding="utf-8").strip()
        mtime = marker_path.stat().st_mtime
    except Exception as e:
        logger.warning(f"Unreadable {marker_path}, treating as 4.0.x marker: {e}")
        content, mtime = "{}", time.time()

    if not content:
        return _new_manifest(plugin_disabled=False)

    try:
        data = json.loads(content)
    except Exception:
        data = None
    if isinstance(data, dict) and data.get("version") == _MANIFEST_VERSION:
        manifest = _new_manifest(plugin_disabled=bool(data.get("plugin_disabled")))
        since = data.get("since")
        manifest["since"] = since if _is_number(since) else None
        for key in ("processed", "profiles"):
            records = data.get(key)
            if isinstance(records, dict):
                manifest[key] = {
                    k: v for k, v in records.items()
                    if isinstance(k, str) and isinstance(v, dict)
                }
        return manifest

    # 4.0.x stats marker: an import ran at `mtime` and the plugin was disabled.
    return _new_manifest(plugin_disabled=True, since=mtime)


def _write_manifest(marker_path: Path, manifest: dict) -> None:
    try:
        marker_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = marker_path.with_name(marker_path.name + ".tmp")
        tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, marker_path)
    except Exception as e:
        logger.warning(f"Failed to write hippocampus manifest {marker_path}: {e}")


# ── Small helpers ────────────────────────────────────────────────────────────


def _is_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v == v


def _file_hash(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()[:16]


def _bucket(ns: Namespace) -> str:
    """Stats key for a namespace."""
    _, _, folder, base_dir = ns
    if base_dir == "global/self":
        return "global_self"
    if base_dir == "global/skills" or folder == "skills":
        return "skills"
    if base_dir:
        return "global_facts"
    return "reflections" if folder == "reflections" else "facts"


def _parse_source_time(source: dict) -> Optional[float]:
    """`source.time` is the ISO creation time both plugins stamp on add."""
    raw = source.get("time") if isinstance(source, dict) else None
    if not isinstance(raw, str) or not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    ts = dt.timestamp()
    return ts if ts > 0 else None


def _load_deleted_entities(since: Optional[float]) -> set:
    """Entities deleted from the KiraOS WebUI after hippocampus data first landed.

    `api_delete_entity` moves the entity dir to
    `archive/_full_entities/{type}_{quoted_id}_{unix_ts}`. Deletions from before
    `since` concern KiraOS-native data only, so the hippocampus copy still gets
    imported; later ones mean the user removed data we already brought over.
    """
    if since is None:
        return set()
    root = Path(get_archive_dir()) / "_full_entities"
    if not root.is_dir():
        return set()
    deleted = set()
    for item in root.iterdir():
        stem, _, ts = item.name.rpartition("_")
        etype, _, segment = stem.partition("_")
        if not item.is_dir() or not ts.isdigit() or not segment:
            continue
        # The dir name has whole seconds; a deletion in the same second as
        # `since` counts as after it (never resurrect over a missed delete).
        if etype in (ENTITY_USER, ENTITY_GROUP, ENTITY_CHANNEL) and int(ts) >= int(since):
            deleted.add((etype, _path_segment_to_id(segment)))
    return deleted


# ── Hippocampus source tree ──────────────────────────────────────────────────


def _parse_entity_dir(name: str) -> Optional[Tuple[str, str]]:
    """`{type}_{quote(id)}` (hippocampus paths.py) → (entity_type, entity_id)."""
    etype, sep, segment = name.partition("_")
    if not sep or etype not in (ENTITY_USER, ENTITY_GROUP, ENTITY_CHANNEL):
        return None
    try:
        return etype, _validate_id(_path_segment_to_id(segment))
    except ValueError:
        return None


def _iter_profiles(hippo_root: Path) -> Iterator[Tuple[Path, str, str]]:
    entities = hippo_root / "entities"
    if not entities.is_dir():
        return
    for entity_dir in sorted(entities.iterdir()):
        parsed = _parse_entity_dir(entity_dir.name) if entity_dir.is_dir() else None
        if parsed is None:
            continue
        profile_json = entity_dir / "profile.json"
        if profile_json.is_file():
            yield profile_json, parsed[0], parsed[1]


def _iter_memory_files(hippo_root: Path) -> Iterator[Path]:
    """Memory files under entities/ and global/ (TOML, plus legacy JSON)."""
    for top in ("entities", "global"):
        base = hippo_root / top
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file() or path.suffix not in (".toml", ".json"):
                continue
            if path.name == "profile.json" or "chat_memory" in path.name:
                continue
            yield path


def _namespace_for(path: Path, hippo_root: Path) -> Optional[Namespace]:
    """KiraOS namespace of a source file, or None if outside the known layout.

    Same rule as `MemoryIndex._parse_path` (used by the startup rebuild), so an
    imported memory lands where the next rebuild expects it. The extra shape
    checks reject files that rule would mis-file (nested dirs, bad entity ids).
    """
    parts = path.relative_to(hippo_root).parts
    entity_id, entity_type, folder, base_dir = MemoryIndex._parse_path(
        str(path), str(hippo_root)
    )
    if parts[0] == "entities":
        parsed = _parse_entity_dir(parts[1]) if len(parts) == 4 else None
        if parsed is None or folder not in _ENTITY_FOLDERS:
            return None
        entity_type, entity_id = parsed
    elif len(parts) not in (3, 4) or (len(parts) == 4 and not base_dir.count("/")):
        return None
    try:
        TomlTreeStore._validate_memory_id(path.stem)
    except ValueError:
        return None
    return entity_type, entity_id, folder, base_dir


class _HippoMeta:
    """Runtime meta from the hippocampus SQLite index (opened read-only).

    Hippocampus keys rows by bare `id` (global PK), so two entities sharing a
    slug share one row. A row is trusted for a file when its `file_path`, taken
    relative to the memory root, is that file's path (survives a moved data
    root), or — for rows without a usable `file_path` — when it has the same id
    and the same content hash.

    Its `timestamp` is the first-insert time and reliable. `last_accessed` /
    `access_count` are reset by hippocampus's own startup rebuild, so they are
    only a lower bound; merging takes the max, which never loses information.
    """

    def __init__(self, hippo_root: Path):
        self._db_path = hippo_root / "memory_index.db"
        self._by_rel: Optional[Dict[str, dict]] = None  # loaded on first lookup
        self._unplaced: Dict[str, dict] = {}  # rows whose file_path is unusable

    def _index(self, rows: List[dict]) -> None:
        self._by_rel = {}
        for row in rows:
            rel = self._row_rel(row.get("file_path"))
            if rel:
                self._by_rel[rel] = row
            elif row.get("id"):
                self._unplaced[str(row["id"])] = row

    @staticmethod
    def _row_rel(file_path) -> Optional[str]:
        """`file_path` relative to the memory root, wherever that root was."""
        parts = str(file_path or "").replace("\\", "/").split("/")
        for i in range(len(parts) - 1, -1, -1):
            if parts[i] in ("entities", "global") and len(parts) - i >= 3:
                return "/".join(parts[i:])
        return None

    def _load(self) -> List[dict]:
        db_path = self._db_path
        if not db_path.is_file():
            return []
        # A WAL database opened `mode=ro` leaves -shm/-wal files behind in the
        # source tree. With no -wal file everything is checkpointed into the
        # main file, so `immutable` reads it without creating anything; only a
        # live -wal (hippocampus running or crashed) needs the WAL-aware open.
        queries = ("mode=ro&immutable=1", "mode=ro")
        if db_path.with_name(db_path.name + "-wal").exists():
            queries = queries[::-1]
        for query in queries:
            try:
                conn = sqlite3.connect(f"{db_path.as_uri()}?{query}", uri=True)
                try:
                    conn.row_factory = sqlite3.Row
                    rows = [dict(r) for r in conn.execute("SELECT * FROM memories")]
                finally:
                    conn.close()
                logger.info(f"Loaded runtime meta for {len(rows)} hippocampus memories")
                return rows
            except sqlite3.Error as e:
                logger.debug(f"Hippocampus index open ({query}) failed: {e}")
        logger.warning(
            f"Hippocampus index {db_path} unreadable — falling back to source.time / mtime"
        )
        return []

    def lookup(self, rel: str, ids: List[str], text_hash: str) -> Optional[dict]:
        if self._by_rel is None:
            self._index(self._load())
        row = self._by_rel.get(rel)
        if row is not None:
            return row
        # A placed row belongs to the file at its own path; reusing it here
        # would hand one entity another entity's meta for a shared slug.
        for mem_id in ids:
            row = self._unplaced.get(mem_id)
            if row is None:
                continue
            row_hash = row.get("content_hash") or ""
            if not row_hash and isinstance(row.get("raw_text"), str):
                row_hash = MemoryIndex.content_hash(row["raw_text"])
            if row_hash == text_hash:
                return row
        return None


class _Source:
    """One parsed hippocampus memory file."""

    __slots__ = ("id", "alt_ids", "text", "type", "importance", "tags", "source",
                 "embedded", "file_hash", "text_hash")


def _read_source(path: Path, folder: str, raw: bytes) -> Optional[_Source]:
    """Parse a TOML (or legacy JSON) memory; None if unusable."""
    try:
        if path.suffix == ".toml":
            data = tomllib.loads(raw.decode("utf-8"))
            text, embedded = data.get("text"), data.get("meta")
            importance, tags, source = data.get("importance"), data.get("tags"), data.get("source")
        else:
            # Pre-TOML hippocampus format: {id, type, content: {raw_text}, meta: {...}}
            data = json.loads(raw.decode("utf-8"))
            content = data.get("content") if isinstance(data, dict) else None
            embedded = data.get("meta") if isinstance(data, dict) else None
            text = content.get("raw_text") if isinstance(content, dict) else None
            meta = embedded if isinstance(embedded, dict) else {}
            importance, tags, source = meta.get("importance"), meta.get("tags"), meta.get("source")
    except (UnicodeDecodeError, ValueError, tomllib.TOMLDecodeError) as e:
        logger.warning(f"Unreadable hippocampus memory {path}: {e}")
        return None
    if not isinstance(data, dict) or not isinstance(text, str) or not text.strip():
        logger.warning(f"Hippocampus memory {path} has no text, skipping")
        return None

    src = _Source()
    # The file name is the storage key in both plugins; the inner `id` is
    # only used to find the matching hippocampus index row.
    src.id = path.stem
    # 4.0.x imported under the inner id, so it also names a possible KiraOS copy.
    src.alt_ids = [src.id]
    inner_id = data.get("id")
    if isinstance(inner_id, str) and inner_id != src.id:
        try:
            src.alt_ids.append(TomlTreeStore._validate_memory_id(inner_id))
        except ValueError:
            pass
    src.text = text
    mem_type = data.get("type")
    if not isinstance(mem_type, str) or not mem_type:
        mem_type = "reflection" if folder == "reflections" else "fact"
    src.type = mem_type
    try:
        src.importance = max(1, min(10, int(importance)))
    except (TypeError, ValueError):
        src.importance = 5
    src.tags = normalize_tags(tags)
    src.source = dict(source) if isinstance(source, dict) else {}
    src.embedded = embedded if isinstance(embedded, dict) else {}
    src.file_hash = _file_hash(raw)
    src.text_hash = MemoryIndex.content_hash(text)
    return src


def _source_meta(src: _Source, row: Optional[dict], path: Path) -> List[float]:
    """[timestamp, last_accessed, access_count]: earliest creation, largest counts."""
    created = [
        v for v in (
            (row or {}).get("timestamp"),
            src.embedded.get("timestamp"),
            _parse_source_time(src.source),
        )
        if _is_number(v) and v > 0
    ]
    if not created:
        # mtime is the last write, so only an upper bound on creation time.
        created = [path.stat().st_mtime]
    ts = float(min(created))
    accessed = [
        v for v in ((row or {}).get("last_accessed"), src.embedded.get("last_accessed"))
        if _is_number(v) and v > 0
    ]
    la = float(max(accessed + [ts]))
    counts = [
        v for v in ((row or {}).get("access_count"), src.embedded.get("access_count"))
        if _is_number(v) and v >= 0
    ]
    ac = int(max(counts)) if counts else 0
    return [ts, la, ac]


# ── Profiles ─────────────────────────────────────────────────────────────────


def _union(*lists) -> list:
    """Order-preserving union of the non-empty strings in `lists`."""
    out: list = []
    for lst in lists:
        for item in lst if isinstance(lst, list) else []:
            if isinstance(item, str) and item.strip() and item not in out:
                out.append(item)
    return out


def _merge_profile(profile, data: dict) -> None:
    """Merge a hippocampus profile dict into a KiraOS `EntityProfile` in place.

    Nothing is dropped: lists are unioned, a name that loses goes to `aliases`,
    counters take the max. On conflicts the side with the later
    `last_interaction` wins — a fresh KiraOS profile (0) always loses, a
    profile KiraOS kept updating after the import keeps its values.
    """
    hippo_ts = data.get("last_interaction")
    hippo_ts = float(hippo_ts) if _is_number(hippo_ts) else 0.0
    hippo_newer = hippo_ts > (profile.last_interaction or 0.0)

    def pick(cur, new):
        if not isinstance(new, str) or not new.strip():
            return cur
        if not cur:
            return new
        return new if hippo_newer else cur

    candidates = [profile.name, profile.nickname, data.get("name"), data.get("nickname")]
    for key in ("name", "nickname", "description", "platform"):
        setattr(profile, key, pick(getattr(profile, key), data.get(key)))
    displaced = [
        n for n in candidates
        if isinstance(n, str) and n.strip() and n not in (profile.name, profile.nickname)
    ]

    profile.traits = _union(profile.traits, data.get("traits"))
    profile.facts = _union(profile.facts, data.get("facts"))
    profile.aliases = _union(profile.aliases, data.get("aliases"), displaced)
    for key in ("preferences", "relationships", "metadata"):
        cur = getattr(profile, key)
        cur = cur if isinstance(cur, dict) else {}
        new = data.get(key) if isinstance(data.get(key), dict) else {}
        setattr(profile, key, {**cur, **new} if hippo_newer else {**new, **cur})

    count = data.get("interaction_count")
    if _is_number(count):
        profile.interaction_count = max(int(profile.interaction_count or 0), int(count))
    profile.last_interaction = max(profile.last_interaction or 0.0, hippo_ts)


async def _reconcile_profile(
    profile_json: Path,
    rel: str,
    entity_type: str,
    entity_id: str,
    profiles: Dict[str, dict],
    profile_store: EntityProfileStore,
    deleted: set,
) -> bool:
    """Merge one hippocampus profile.json; True if KiraOS's profile was updated.

    Re-merged whenever the source file changes. Raises (→ retried next launch)
    if the merged profile could not be saved.
    """
    if (entity_type, entity_id) in deleted:
        return False
    raw = profile_json.read_bytes()
    file_hash = _file_hash(raw)
    if profiles.get(rel, {}).get("hash") == file_hash:
        return False
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        logger.warning(f"Unreadable hippocampus profile {rel}: {e}")
        data = None
    if not isinstance(data, dict):
        profiles[rel] = {"hash": file_hash, "state": _ST_INVALID}
        return False

    # `get_profile` hands back an in-memory default when the KiraOS file exists
    # but cannot be parsed; saving that would wipe the user's profile.
    target = Path(get_entity_profile_path(entity_id, entity_type))
    if target.exists():
        json.loads(target.read_text(encoding="utf-8"))

    # Not `update_profile`: it stamps `last_interaction = now`, which would make
    # every migrated entity look active today.
    async with await profile_store._get_entity_lock(entity_id, entity_type):
        profile = await profile_store.get_profile(entity_id, entity_type)
        _merge_profile(profile, data)
        if not await profile_store.save_profile(profile):
            raise RuntimeError(f"could not save merged profile for {rel}")
    profiles[rel] = {"hash": file_hash}
    return True


# ── Memories ─────────────────────────────────────────────────────────────────


def _loc(ns: Namespace) -> dict:
    entity_type, entity_id, folder, base_dir = ns
    return {"entity_id": entity_id, "entity_type": entity_type,
            "folder": folder, "base_dir": base_dir}


def _kiraos_file(ns: Namespace, mem_id: str) -> Path:
    loc = _loc(ns)
    return Path(TomlTreeStore._resolve_dir(**loc)) / f"{mem_id}.toml"


def _archive_file(ns: Namespace, mem_id: str) -> Path:
    """Same naming as `TomlTreeStore.archive_memory`."""
    entity_type, entity_id, folder, base_dir = ns
    if base_dir:
        prefix = base_dir.replace("/", "_").replace("\\", "_")
    else:
        prefix = f"{entity_type}__{_id_to_path_segment(entity_id)}"
    return Path(get_archive_dir()) / f"{prefix}__{folder}__{mem_id}.toml"


def _candidate_ids(src: _Source) -> List[str]:
    """Ids a KiraOS copy of `src` can have: its own, or the `{id}_{hash8}` that
    `add_memory` assigns when the slug was already taken by a different text."""
    return src.alt_ids + [f"{src.id}_{src.text_hash[:8]}"]


def _is_ours(text, tags, src: _Source) -> bool:
    """Same text, or our import edited later by KiraOS (e.g. a fact merge)."""
    if isinstance(text, str) and MemoryIndex.content_hash(text) == src.text_hash:
        return True
    return isinstance(tags, list) and _IMPORT_TAG in tags


async def _get_live(tree_store, ns: Namespace, mem_id: str):
    """KiraOS memory `mem_id` in `ns`; None if absent. Raises if the file exists
    but cannot be read, so an I/O hiccup is retried instead of read as a delete."""
    if not mem_id or not _kiraos_file(ns, mem_id).is_file():
        return None
    mem = await tree_store.get_memory(mem_id, **_loc(ns))
    if mem is None:
        raise RuntimeError(f"KiraOS memory {mem_id} exists but could not be read")
    return mem


async def _find_existing(tree_store, ns: Namespace, src: _Source):
    """A KiraOS copy of `src` already in its namespace (4.0.x import, an earlier
    reconcile that lost its manifest, or an identical KiraOS-native memory)."""
    for mem_id in _candidate_ids(src):
        mem = await _get_live(tree_store, ns, mem_id)
        if mem is not None and _is_ours(mem.text, mem.tags, src):
            return mem
    row = await asyncio.to_thread(
        lambda: tree_store.index.find_by_hash(src.text_hash, **_loc(ns))
    )
    if row and row.get("id"):
        return await _get_live(tree_store, ns, str(row["id"]))
    return None


def _archived(ns: Namespace, src: _Source) -> bool:
    """Whether KiraOS archived its copy of `src` (decay, WebUI or tool delete)."""
    for mem_id in _candidate_ids(src):
        path = _archive_file(ns, mem_id)
        if not path.is_file():
            continue
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"Unreadable KiraOS archive {path}, not re-importing: {e}")
            return True
        if _is_ours(data.get("text"), data.get("tags"), src):
            return True
    return False


def _merged_meta(cur: Tuple, new: List[float]) -> Optional[Tuple[float, float, int]]:
    """Earliest timestamp, latest access, largest count; None if `cur` already is."""
    cur_ts, cur_la, cur_ac = (v if _is_number(v) else 0 for v in cur)
    ts = min(cur_ts, new[0]) if cur_ts > 0 else new[0]
    merged = (float(ts), float(max(cur_la, new[1], ts)), int(max(cur_ac, new[2])))
    return None if merged == (cur_ts, cur_la, cur_ac) else merged


async def _write(tree_store, mem, meta: Optional[Tuple]) -> None:
    if meta is not None:
        mem.meta.update(timestamp=meta[0], last_accessed=meta[1], access_count=meta[2])
    if not await tree_store.update_memory(mem):
        raise RuntimeError(f"could not update KiraOS memory {mem.id}")


def _record(state: str, src: _Source, mem=None) -> dict:
    rec = {"state": state, "hash": src.file_hash}
    if mem is not None:
        rec["id"] = mem.id
        rec["text_hash"] = MemoryIndex.content_hash(mem.text)
    return rec


async def _reconcile_memory(
    path: Path,
    rel: str,
    ns: Namespace,
    record: Optional[dict],
    tree_store,
    hippo_meta: _HippoMeta,
    deleted: set,
) -> Tuple[str, dict]:
    """Reconcile one hippocampus memory file into KiraOS.

    Returns (outcome, manifest record); outcome is `_ST_IMPORTED`, `_ST_MERGED`,
    `_UP_TO_DATE` or `_SKIPPED`. Raises on I/O or store errors, leaving the file
    unrecorded so the next launch retries it.
    """
    entity_type, entity_id, folder, _ = ns
    state = (record or {}).get("state")
    if state in _TERMINAL_STATES:
        return _SKIPPED, record
    raw = path.read_bytes()
    file_hash = _file_hash(raw)
    if record and record.get("hash") == file_hash:
        return (_UP_TO_DATE if state in _OK_STATES else _SKIPPED), record
    if entity_type and (entity_type, entity_id) in deleted:
        return _SKIPPED, {"state": _ST_ENTITY_DELETED, "hash": file_hash}
    src = _read_source(path, folder, raw)
    if src is None:
        return _SKIPPED, {"state": _ST_INVALID, "hash": file_hash}
    meta = _source_meta(src, hippo_meta.lookup(rel, src.alt_ids, src.text_hash), path)

    if state in _OK_STATES:
        # Already reconciled, but the source changed since (hippocampus was
        # re-enabled and edited it).
        mem = await _get_live(tree_store, ns, record.get("id", ""))
        if mem is None:
            # Gone from KiraOS after the import — deleted or decayed there.
            gone = _ST_ARCHIVED if _archived(ns, src) else _ST_DELETED
            return _SKIPPED, {**record, "state": gone, "hash": file_hash}
        changed = False
        if (MemoryIndex.content_hash(mem.text) == record.get("text_hash")
                and mem.text != src.text):
            # Untouched in KiraOS since the import: take the hippocampus edit.
            # Otherwise KiraOS's own edit wins and only meta is merged.
            mem.text, mem.type, mem.importance = src.text, src.type, src.importance
            mem.tags = _union(mem.tags, src.tags)
            changed = True
        merged = _merged_meta((mem.timestamp, mem.last_accessed, mem.access_count), meta)
        if not changed and merged is None:
            return _UP_TO_DATE, _record(_ST_MERGED, src, mem)
        await _write(tree_store, mem, merged)
        return _ST_MERGED, _record(_ST_MERGED, src, mem)

    # Not recorded yet: first run, an upgrade from the 4.0.x one-shot import,
    # or a file that appeared in hippocampus after an earlier run.
    if _archived(ns, src):
        return _SKIPPED, {"state": _ST_ARCHIVED, "hash": file_hash}
    mem = await _find_existing(tree_store, ns, src)
    if mem is not None:
        merged = _merged_meta((mem.timestamp, mem.last_accessed, mem.access_count), meta)
        if merged is None:
            return _UP_TO_DATE, _record(_ST_MERGED, src, mem)
        await _write(tree_store, mem, merged)
        return _ST_MERGED, _record(_ST_MERGED, src, mem)

    mem = await tree_store.add_memory(
        content_text=src.text,
        memory_type=src.type,
        importance=src.importance,
        tags=_union(src.tags, [_IMPORT_TAG]),
        source=src.source,
        semantic_id=src.id,
        **_loc(ns),
    )
    # `add_memory` stamps now / 0; put the aged meta back so decay and
    # retention scoring see the memory's real age.
    await _write(tree_store, mem, tuple(meta))
    return _ST_IMPORTED, _record(_ST_IMPORTED, src, mem)


# ── Archive & plugin ─────────────────────────────────────────────────────────


def _copy_archive(hippo_root: Path) -> None:
    """Copy hippocampus `archive/` into KiraOS `archive/hippocampus/`.

    Incremental and never overwrites, so files archived later in hippocampus
    are picked up and nothing already in KiraOS's archive is replaced. Kept out
    of the flat KiraOS archive namespace on purpose: these are hippocampus's
    archived memories, not KiraOS tombstones.
    """
    src_root = hippo_root / "archive"
    if not src_root.is_dir():
        return
    dest_root = Path(get_archive_dir()) / "hippocampus"
    copied = 0
    for item in sorted(src_root.rglob("*")):
        if not item.is_file():
            continue
        target = dest_root / item.relative_to(src_root)
        if target.exists():
            continue
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)
            copied += 1
        except OSError as e:
            logger.warning(f"Failed to copy hippocampus archive file {item}: {e}")
    if copied:
        logger.info(f"Copied {copied} hippocampus archive file(s) to {dest_root}")


async def _disable_hippocampus_plugin(ctx) -> bool:
    """Disable the hippocampus plugin; True once it is off (False → retry)."""
    try:
        mgr = getattr(ctx, "plugin_mgr", None)
        if mgr is None:
            return False
        if not mgr.is_plugin_enabled(_HIPPOCAMPUS_PLUGIN_ID):
            return True
        await mgr.set_plugin_enabled(_HIPPOCAMPUS_PLUGIN_ID, False)
        logger.warning(
            f"Hippocampus plugin ({_HIPPOCAMPUS_PLUGIN_ID}) auto-disabled after migration. "
            "Its data is untouched; re-enabling it later is respected, and any "
            "memories it adds are picked up on the next KiraOS launch."
        )
        return True
    except Exception as e:
        logger.warning(f"Failed to auto-disable hippocampus plugin: {e}")
        return False

