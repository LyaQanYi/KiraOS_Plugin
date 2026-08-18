"""One-shot migration from kira_plugin_hippocampus_memory into unified KiraOS storage.

Discovers hippocampus data under `data/plugin_data/kira_plugin_hippocampus_memory/memory/`,
imports entities (profiles + facts/reflections) and global facts into the unified TOML+SQLite
store, preserves SQLite runtime meta (timestamps, access_count), then marks the plugin as
disabled to prevent double-feeding.

Design:
- Read-only on source (never renames, never writes back)
- Idempotent (marker-gated + content-hash dedup, safe to retry)
- Graceful degradation (bad TOML → warn + skip, not crash)
- Namespace collision strategy: merge profiles (union traits/facts), skip duplicate memories
  (by content hash)
- Runtime meta preservation: reads hippocampus SQLite for timestamps/access_count, writes to
  KiraOS SQLite + embeds in TOML [meta] section for durability across index rebuilds
"""

import asyncio
import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Optional, Dict, Set

try:
    import tomllib  # Python 3.11+
except ImportError:
    import tomli as tomllib  # Python 3.10 fallback

from core.logging_manager import get_logger
from core.utils.path_utils import get_data_path

from .memory_paths import (
    get_entity_profile_path,
    get_entity_folder,
    ensure_entity_dirs,
    get_global_dir,
    ENTITY_USER,
    ENTITY_GROUP,
    ENTITY_CHANNEL,
)
from .entity_profile import EntityProfile, EntityProfileStore
from .toml_tree_store import Memory

logger = get_logger("kiraos_hippocampus_migration", "cyan")

_MARKER_NAME = ".hippocampus_migrated"
_HIPPOCAMPUS_PLUGIN_ID = "kira_plugin_hippocampus_memory"


async def migrate_hippocampus_if_needed(
    tree_store, profile_store: EntityProfileStore, ctx
) -> dict:
    """Import hippocampus data if present and not yet migrated.

    Returns:
        {"migrated": bool, "entities": int, "facts": int, "reflections": int, "global_facts": int}
    """
    marker_path = get_data_path() / "memory" / _MARKER_NAME
    if marker_path.exists():
        return {"migrated": False, "entities": 0, "facts": 0, "reflections": 0, "global_facts": 0}

    # Hippocampus stores data under data/plugin_data/<plugin_id>/memory/
    hippo_root = get_data_path() / "plugin_data" / _HIPPOCAMPUS_PLUGIN_ID / "memory"
    if not hippo_root.exists():
        # No hippocampus data; drop marker to skip future checks
        try:
            marker_path.parent.mkdir(parents=True, exist_ok=True)
            marker_path.touch()
        except Exception:
            pass
        return {"migrated": False, "entities": 0, "facts": 0, "reflections": 0, "global_facts": 0}

    logger.info(f"Hippocampus data detected at {hippo_root}, starting migration...")

    stats = {"entities": 0, "facts": 0, "reflections": 0, "global_facts": 0}
    migrated_hashes: Set[str] = set()

    # Load hippocampus SQLite runtime meta (timestamps, access_count)
    sqlite_meta = _load_hippocampus_sqlite_meta(hippo_root)

    # === 1. Import entities (user/group/channel) ===
    entities_dir = hippo_root / "entities"
    if entities_dir.exists():
        for entity_dir in entities_dir.iterdir():
            if not entity_dir.is_dir():
                continue
            # Parse entity_type_entity_id (e.g., user_123, group_456)
            entity_type, entity_id = _parse_entity_dir_name(entity_dir.name)
            if not entity_type or not entity_id:
                logger.warning(f"Skipping malformed entity dir: {entity_dir.name}")
                continue

            # Import profile
            profile_json = entity_dir / "profile.json"
            if profile_json.exists():
                await _import_profile(profile_json, entity_id, entity_type, profile_store)
                stats["entities"] += 1

            # Import facts + reflections
            for folder in ["facts", "reflections"]:
                folder_dir = entity_dir / folder
                if folder_dir.exists():
                    count = await _import_memories_dir(
                        folder_dir, tree_store, entity_id, entity_type, folder,
                        sqlite_meta, migrated_hashes
                    )
                    if folder == "facts":
                        stats["facts"] += count
                    else:
                        stats["reflections"] += count

    # === 2. Import global facts ===
    global_facts_dir = hippo_root / "global" / "facts"
    if global_facts_dir.exists():
        count = await _import_memories_dir(
            global_facts_dir, tree_store, "", "", "facts",
            sqlite_meta, migrated_hashes
        )
        stats["global_facts"] += count

    # === 3. Auto-disable hippocampus plugin to prevent double-feeding ===
    await _disable_hippocampus_plugin(ctx)

    # === 4. Drop marker ===
    try:
        marker_path.parent.mkdir(parents=True, exist_ok=True)
        marker_path.write_text(
            json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as e:
        logger.warning(f"Failed to write migration marker: {e}")

    logger.info(
        f"Hippocampus migration complete: {stats['entities']} entities, "
        f"{stats['facts']} facts, {stats['reflections']} reflections, "
        f"{stats['global_facts']} global facts"
    )
    return {"migrated": True, **stats}


def _parse_entity_dir_name(name: str) -> tuple[str, str]:
    """Parse 'user_123' -> ('user', '123')."""
    parts = name.split("_", 1)
    if len(parts) != 2:
        return "", ""
    entity_type, entity_id = parts
    if entity_type not in (ENTITY_USER, ENTITY_GROUP, ENTITY_CHANNEL):
        return "", ""
    return entity_type, entity_id


def _load_hippocampus_sqlite_meta(hippo_root: Path) -> Dict[str, Dict]:
    """Load runtime meta (timestamps, access_count) from hippocampus SQLite.

    Returns: {storage_key: {timestamp, last_accessed, access_count}}
    """
    db_path = hippo_root / "memory_index.db"
    if not db_path.exists():
        logger.warning(f"Hippocampus SQLite not found at {db_path}, timestamps will default to now")
        return {}

    meta_map = {}
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT storage_key, timestamp, last_accessed, access_count
            FROM memories
        """).fetchall()
        for row in rows:
            meta_map[row["storage_key"]] = {
                "timestamp": row["timestamp"],
                "last_accessed": row["last_accessed"],
                "access_count": row["access_count"],
            }
        conn.close()
        logger.info(f"Loaded {len(meta_map)} memory meta entries from hippocampus SQLite")
    except Exception as e:
        logger.error(f"Failed to read hippocampus SQLite: {e}")
    return meta_map


def _compute_content_hash(text: str, importance: int, tags: list) -> str:
    """Compute a stable hash for deduplication (same content = same hash)"""
    canonical = f"{text.strip()}|{importance}|{sorted(tags)}"
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def _import_profile(
    profile_json: Path, entity_id: str, entity_type: str, profile_store: EntityProfileStore
):
    """Merge hippocampus profile.json into KiraOS profile (union traits/facts/aliases)."""
    try:
        data = json.loads(profile_json.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning(f"Failed to parse {profile_json}: {e}")
        return

    # Get existing profile or create new one
    existing = await profile_store.get_profile(entity_id, entity_type)

    # Union merge: traits, facts, aliases (deduplicated)
    merged_traits = list(set(existing.traits) | set(data.get("traits", [])))
    merged_facts = list(set(existing.facts) | set(data.get("facts", [])))
    merged_aliases = list(set(existing.aliases) | set(data.get("aliases", [])))

    # Preferences: hippocampus wins for keys it defines
    merged_prefs = {**existing.preferences, **data.get("preferences", {})}

    # Relationships: hippocampus wins
    merged_rels = {**existing.relationships, **data.get("relationships", {})}

    # Metadata: take max interaction_count, most recent last_interaction
    interaction_count = max(existing.interaction_count, data.get("interaction_count", 0))
    last_interaction = max(existing.last_interaction, data.get("last_interaction", 0.0))

    # Update profile
    await profile_store.update_profile(
        entity_id,
        entity_type,
        name=data.get("name", existing.name),
        nickname=data.get("nickname", existing.nickname),
        description=data.get("description", existing.description),
        platform=data.get("platform", existing.platform),
        traits=merged_traits,
        preferences=merged_prefs,
        relationships=merged_rels,
        facts=merged_facts,
        aliases=merged_aliases,
        interaction_count=interaction_count,
        last_interaction=last_interaction,
    )
    logger.debug(f"Merged profile for {entity_type}:{entity_id}")


async def _import_memories_dir(
    memories_dir: Path,
    tree_store,
    entity_id: str,
    entity_type: str,
    folder: str,
    sqlite_meta: Dict[str, Dict],
    migrated_hashes: Set[str],
) -> int:
    """Import all .toml files from a memories directory (facts or reflections).

    Args:
        memories_dir: Path to facts/ or reflections/ directory
        tree_store: TomlTreeStore instance
        entity_id: Entity ID (empty for global)
        entity_type: Entity type (empty for global)
        folder: "facts" or "reflections"
        sqlite_meta: Hippocampus SQLite meta mapping
        migrated_hashes: Set of already-migrated content hashes (for idempotency)

    Returns:
        Number of memories successfully imported
    """
    count = 0
    for toml_file in memories_dir.glob("*.toml"):
        try:
            data = tomllib.loads(toml_file.read_text(encoding="utf-8"))

            # Check if already migrated (by content hash)
            text = data.get("text", "")
            importance = data.get("importance", 5)
            tags = data.get("tags", [])
            if not isinstance(tags, list):
                tags = []

            content_hash = _compute_content_hash(text, importance, tags)
            if content_hash in migrated_hashes:
                logger.debug(f"Skipping duplicate: {toml_file.name}")
                continue

            # Add [hippocampus-import] tag for traceability
            if "hippocampus-import" not in tags:
                tags.append("hippocampus-import")

            # Build storage_key to lookup SQLite meta
            mem_id = data.get("id", "")
            storage_key = f"{entity_type}\x01{entity_id}\x01{folder}\x01\x01{mem_id}"
            runtime_meta = sqlite_meta.get(storage_key, {})

            # Merge TOML embedded [meta] with SQLite meta (SQLite wins)
            embedded_meta = data.get("meta", {})
            if isinstance(embedded_meta, dict):
                final_meta = {**embedded_meta, **runtime_meta}
            else:
                final_meta = runtime_meta.copy()

            # If no timestamp at all, use current time
            if "timestamp" not in final_meta:
                final_meta["timestamp"] = time.time()
            if "last_accessed" not in final_meta:
                final_meta["last_accessed"] = final_meta["timestamp"]
            if "access_count" not in final_meta:
                final_meta["access_count"] = 0

            # Create Memory object
            memory = Memory(
                id=mem_id,
                type=data.get("type", "fact"),
                text=text,
                importance=importance,
                tags=tags,
                source=data.get("source", {}),
                meta=final_meta,
                _entity_id=entity_id,
                _entity_type=entity_type,
                _folder=folder,
                _base_dir="" if entity_id else "global",
            )

            # Save via tree_store (handles TOML write + SQLite index)
            await tree_store.save_memory(memory)
            migrated_hashes.add(content_hash)
            count += 1
        except Exception as e:
            logger.warning(f"Failed to import {toml_file}: {e}")
    return count


async def _disable_hippocampus_plugin(ctx):
    """Auto-disable hippocampus plugin to prevent double-feeding."""
    try:
        mgr = ctx.plugin_mgr
        if mgr is None:
            return
        if mgr.is_plugin_enabled(_HIPPOCAMPUS_PLUGIN_ID):
            await mgr.set_plugin_enabled(_HIPPOCAMPUS_PLUGIN_ID, False)
            logger.warning(
                f"Hippocampus plugin ({_HIPPOCAMPUS_PLUGIN_ID}) auto-disabled after migration. "
                "Data remains untouched for rollback. Re-enable manually if needed."
            )
    except Exception as e:
        logger.warning(f"Failed to auto-disable hippocampus plugin: {e}")
