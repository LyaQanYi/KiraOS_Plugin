"""
Hippocampus Plugin Data Migrator

One-time importer for kira_plugin_hippocampus_memory data into the unified
KiraOS_Plugin memory structure. Runs automatically on first launch if
`auto_migrate_legacy_db` is enabled and hippocampus plugin data is detected.

Migration scope:
- TOML facts/reflections → KiraOS_Plugin TOML tree
- profile.json → KiraOS_Plugin entity profiles
- SQLite meta (timestamps, access_count) → KiraOS_Plugin memory_index.db

Safety:
- Read-only on hippocampus data (never modifies source files)
- Idempotent (can be re-run safely, skips already-migrated entries)
- Atomic per-entity (if one entity fails, others still complete)
- Full audit log of what was migrated

Non-migration:
- `sender_cache/` (short-term lookup cache, not durable user data)
- `archives/` (already-forgotten memories, intentionally excluded)
- `_MEMORY.md` index files (regenerated from migrated content)
"""

import asyncio
import hashlib
import json
import os
import shutil
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

try:
    import tomllib  # Python 3.11+
except ImportError:
    import tomli as tomllib  # Python 3.10 fallback

from core.logging_manager import get_logger
from .memory_paths import (
    get_memory_root,
    get_entities_dir,
    get_entity_folder,
    ensure_entity_dirs,
    ENTITY_USER,
    ENTITY_GROUP,
    ENTITY_CHANNEL,
)
from .toml_tree_store import TomlTreeStore, Memory
from .entity_profile import EntityProfileStore, EntityProfile
from .memory_index import MemoryIndex

logger = get_logger("hippocampus_migrator", "yellow")


@dataclass
class MigrationStats:
    """Aggregate migration statistics for reporting"""
    entities_scanned: int = 0
    entities_migrated: int = 0
    facts_migrated: int = 0
    reflections_migrated: int = 0
    profiles_migrated: int = 0
    skipped_duplicates: int = 0
    errors: int = 0
    start_time: float = 0.0
    end_time: float = 0.0

    @property
    def duration_seconds(self) -> float:
        if self.end_time == 0:
            return 0.0
        return self.end_time - self.start_time


def _mask_id(value: str) -> str:
    """Mask entity ID for logging (3-char prefix + sha256[:8])"""
    if not value:
        return "<empty>"
    digest = hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()[:8]
    prefix = value[:3] if len(value) >= 3 else value
    return f"{prefix}***({digest})"


class HippocampusMigrator:
    """One-shot importer for kira_plugin_hippocampus_memory data"""

    def __init__(
        self,
        tree_store: TomlTreeStore,
        profile_store: EntityProfileStore,
        memory_index: MemoryIndex,
    ):
        self.tree_store = tree_store
        self.profile_store = profile_store
        self.memory_index = memory_index
        self.stats = MigrationStats()
        # Track already-migrated content hashes to enable idempotency
        self._migrated_hashes: Set[str] = set()

    def _detect_hippocampus_data_root(self) -> Optional[Path]:
        """Locate the hippocampus plugin data directory.

        Expected structure:
        data/plugin_data/kira_plugin_hippocampus_memory/memory/{entities,global}

        Returns None if not found.
        """
        from .memory_paths import get_data_path

        base = get_data_path() / "plugin_data" / "kira_plugin_hippocampus_memory"
        if not base.exists():
            return None
        memory_root = base / "memory"
        if not memory_root.exists():
            return None
        # Verify it has the expected structure (entities/ or global/)
        if not (memory_root / "entities").exists() and not (memory_root / "global").exists():
            return None
        return memory_root

    def _load_hippocampus_sqlite_meta(self, hippo_root: Path) -> Dict[str, Dict]:
        """Load runtime meta (timestamps, access_count) from hippocampus SQLite.

        Returns: {storage_key: {timestamp, last_accessed, access_count}}
        """
        db_path = hippo_root / "memory_index.db"
        if not db_path.exists():
            logger.warning(f"Hippocampus SQLite not found at {db_path}")
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

    def _list_hippocampus_entities(self, hippo_root: Path) -> List[Tuple[str, str, Path]]:
        """Scan hippocampus entities/ directory for all entity folders.

        Returns: [(entity_id, entity_type, path), ...]
        """
        entities_dir = hippo_root / "entities"
        if not entities_dir.exists():
            return []

        result = []
        for entry in entities_dir.iterdir():
            if not entry.is_dir():
                continue
            # Format: {type}_{id}
            name = entry.name
            if "_" not in name:
                continue
            entity_type, entity_id = name.split("_", 1)
            if entity_type not in {ENTITY_USER, ENTITY_GROUP, ENTITY_CHANNEL}:
                continue
            result.append((entity_id, entity_type, entry))
        return result

    def _read_toml_safe(self, path: Path) -> Optional[dict]:
        """Read a TOML file with error handling"""
        try:
            with open(path, "rb") as f:
                return tomllib.load(f)
        except Exception as e:
            logger.error(f"Failed to read {path}: {e}")
            self.stats.errors += 1
            return None

    def _compute_content_hash(self, text: str, importance: int, tags: list) -> str:
        """Compute a stable hash for deduplication (same content = same hash)"""
        canonical = f"{text.strip()}|{importance}|{sorted(tags)}"
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    async def _migrate_entity_memories(
        self,
        entity_id: str,
        entity_type: str,
        entity_path: Path,
        sqlite_meta: Dict[str, Dict],
    ) -> Tuple[int, int]:
        """Migrate all memories (facts + reflections) for one entity.

        Returns: (facts_count, reflections_count)
        """
        facts_count = 0
        reflections_count = 0

        for folder in ["facts", "reflections"]:
            folder_path = entity_path / folder
            if not folder_path.exists():
                continue

            for toml_file in folder_path.glob("*.toml"):
                data = self._read_toml_safe(toml_file)
                if not data:
                    continue

                # Check if already migrated (by content hash)
                content_hash = self._compute_content_hash(
                    data.get("text", ""),
                    data.get("importance", 5),
                    data.get("tags", []),
                )
                if content_hash in self._migrated_hashes:
                    self.stats.skipped_duplicates += 1
                    continue

                # Build storage_key to lookup SQLite meta
                mem_id = data.get("id", "")
                storage_key = f"{entity_type}\x01{entity_id}\x01{folder}\x01\x01{mem_id}"
                runtime_meta = sqlite_meta.get(storage_key, {})

                # Merge TOML embedded [meta] with SQLite meta (SQLite wins)
                embedded_meta = data.get("meta", {})
                final_meta = {**embedded_meta, **runtime_meta}

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
                    text=data.get("text", ""),
                    importance=data.get("importance", 5),
                    tags=data.get("tags", []),
                    source=data.get("source", {}),
                    meta=final_meta,
                    _entity_id=entity_id,
                    _entity_type=entity_type,
                    _folder=folder,
                    _base_dir="",
                )

                # Save via tree_store (will handle TOML write + SQLite index)
                try:
                    await self.tree_store.save_memory(memory)
                    self._migrated_hashes.add(content_hash)
                    if folder == "facts":
                        facts_count += 1
                    else:
                        reflections_count += 1
                except Exception as e:
                    logger.error(f"Failed to migrate memory {mem_id} for {_mask_id(entity_id)}: {e}")
                    self.stats.errors += 1

        return facts_count, reflections_count

    async def _migrate_entity_profile(
        self,
        entity_id: str,
        entity_type: str,
        entity_path: Path,
    ) -> bool:
        """Migrate entity profile.json to KiraOS_Plugin format.

        Returns: True if migrated, False if skipped/error
        """
        profile_path = entity_path / "profile.json"
        if not profile_path.exists():
            return False

        try:
            with open(profile_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            logger.error(f"Failed to read profile {profile_path}: {e}")
            self.stats.errors += 1
            return False

        # Check if profile already exists in KiraOS_Plugin
        existing = await self.profile_store.get_profile(entity_id, entity_type)
        # If existing has non-default data (interaction_count > 0), skip migration
        if existing.interaction_count > 0 or existing.facts:
            logger.debug(f"Profile for {_mask_id(entity_id)} already exists with data, skipping migration")
            return False

        # Build new profile from hippocampus data
        profile = EntityProfile(
            entity_id=entity_id,
            entity_type=entity_type,
            name=data.get("name", ""),
            nickname=data.get("nickname", ""),
            description=data.get("description", ""),
            platform=data.get("platform", ""),
            traits=data.get("traits", []),
            preferences=data.get("preferences", {}),
            relationships=data.get("relationships", {}),
            facts=data.get("facts", []),
            aliases=data.get("aliases", []),
            interaction_count=data.get("interaction_count", 0),
            last_interaction=data.get("last_interaction", 0.0),
            metadata=data.get("metadata", {}),
        )

        try:
            await self.profile_store.save_profile(profile)
            return True
        except Exception as e:
            logger.error(f"Failed to save migrated profile for {_mask_id(entity_id)}: {e}")
            self.stats.errors += 1
            return False

    async def migrate(self) -> MigrationStats:
        """Run the full migration pipeline.

        Returns: MigrationStats with detailed report
        """
        self.stats.start_time = time.time()
        logger.info("=== Hippocampus Plugin Migration Started ===")

        # 1. Detect hippocampus data root
        hippo_root = self._detect_hippocampus_data_root()
        if not hippo_root:
            logger.info("No hippocampus plugin data found, skipping migration")
            self.stats.end_time = time.time()
            return self.stats

        logger.info(f"Found hippocampus data at: {hippo_root}")

        # 2. Load SQLite runtime meta
        sqlite_meta = self._load_hippocampus_sqlite_meta(hippo_root)

        # 3. Scan all entities
        entities = self._list_hippocampus_entities(hippo_root)
        self.stats.entities_scanned = len(entities)
        logger.info(f"Found {len(entities)} entities to migrate")

        # 4. Migrate each entity
        for entity_id, entity_type, entity_path in entities:
            logger.info(f"Migrating {entity_type}:{_mask_id(entity_id)}...")

            # Migrate memories
            facts, reflections = await self._migrate_entity_memories(
                entity_id, entity_type, entity_path, sqlite_meta
            )
            self.stats.facts_migrated += facts
            self.stats.reflections_migrated += reflections

            # Migrate profile
            profile_migrated = await self._migrate_entity_profile(
                entity_id, entity_type, entity_path
            )
            if profile_migrated:
                self.stats.profiles_migrated += 1

            if facts > 0 or reflections > 0 or profile_migrated:
                self.stats.entities_migrated += 1
                logger.info(f"  ✓ Migrated {facts} facts, {reflections} reflections, profile={profile_migrated}")

        # 5. Final report
        self.stats.end_time = time.time()
        logger.info("=== Hippocampus Migration Complete ===")
        logger.info(f"Duration: {self.stats.duration_seconds:.1f}s")
        logger.info(f"Entities: {self.stats.entities_migrated}/{self.stats.entities_scanned}")
        logger.info(f"Facts: {self.stats.facts_migrated}")
        logger.info(f"Reflections: {self.stats.reflections_migrated}")
        logger.info(f"Profiles: {self.stats.profiles_migrated}")
        logger.info(f"Skipped duplicates: {self.stats.skipped_duplicates}")
        logger.info(f"Errors: {self.stats.errors}")

        return self.stats


async def run_hippocampus_migration(
    tree_store: TomlTreeStore,
    profile_store: EntityProfileStore,
    memory_index: MemoryIndex,
) -> MigrationStats:
    """Convenience wrapper for running migration from lifecycle.py"""
    migrator = HippocampusMigrator(tree_store, profile_store, memory_index)
    return await migrator.migrate()
