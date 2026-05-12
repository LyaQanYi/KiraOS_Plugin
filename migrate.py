"""One-shot migration from the legacy ``kiraos.db`` schema to the new
TOML-tree memory layout.

Triggered automatically by ``main.initialize()`` on first run with the new
plugin version. Idempotent: a sentinel file ``data/memory/.migrated_v3``
prevents a second pass, and the legacy database is renamed to
``kiraos.db.legacy.bak`` after a successful migration so it isn't accidentally
re-imported.

Mapping rules
-------------

``user_profiles`` row (legacy KV) → fact TOML under
``entities/user_<id>/facts/<semantic_id>.toml``:
  - ``text``          = ``f"{memory_key}: {memory_value}"``
  - ``importance``    = category baseline + ``round(confidence * 2)`` (capped 1..10)
                        basic=8 / preference=6 / social=5 / other=4
  - ``tags``          = ``[category]`` (+ ``"ttl"`` if expires_at is set)
  - ``semantic_id``   = slug-sanitised ``memory_key`` (suffix with row hash on collision)
  - ``source.legacy`` = ``{"key": memory_key, "category": ..., "confidence": ..., "updated_at": ...}``

``event_logs`` row → fact TOML with type=fact, tagged as ``event``:
  - ``text``        = ``event_summary``
  - ``importance``  = 5
  - ``tags``        = ``[tag] if tag else ["event"]``
  - ``semantic_id`` = ``f"event_{id}_{created_at_short}"``
  - ``source.legacy`` = ``{"event_id": id, "created_at": ..., "tag": ...}``
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import sqlite3
from typing import Optional

from core.logging_manager import get_logger

from .memory.memory_manager import MemoryManager

logger = get_logger("kiraos_migrate", "green")

SENTINEL_FILENAME = ".migrated_v3"
LEGACY_BACKUP_SUFFIX = ".legacy.bak"

CATEGORY_IMPORTANCE = {
    "basic": 8,
    "preference": 6,
    "social": 5,
    "other": 4,
}


def _slugify(key: str, fallback_hash: str = "") -> str:
    """Best-effort snake_case slug, ASCII-safe.

    For Chinese keys (common in the legacy KV schema) ASCII-only stripping
    yields an empty string, so we fall back to a short hash of the original.
    """
    s = (key or "").strip().lower()
    s = re.sub(r"[^a-z0-9_]+", "_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    if s:
        return s[:40]
    return fallback_hash or "key"


def _short_hash(text: str, n: int = 8) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:n]


def _resolve_legacy_db_path(legacy_db_path: str) -> Optional[str]:
    """Pick which legacy DB to migrate from, if any.

    Honours the explicit path first; if missing, falls back to the historical
    default ``data/memory/user_memory.db`` from older plugin revisions.
    """
    if legacy_db_path and os.path.exists(legacy_db_path):
        return legacy_db_path
    base = os.path.dirname(legacy_db_path) if legacy_db_path else ""
    for cand in ("user_memory.db", "kiraos.db"):
        alt = os.path.join(base, cand) if base else cand
        if alt != legacy_db_path and os.path.exists(alt):
            return alt
    return None


async def migrate_legacy_db_if_needed(
    manager: MemoryManager, legacy_db_path: str
) -> dict:
    """If a legacy SQLite KV database is present and we haven't migrated yet,
    convert every row to a TOML memory and stash a sentinel.

    Returns a stats dict (``profiles``, ``events``, ``skipped``, ``status``)
    that the plugin logs.
    """
    from .memory.memory_paths import MEMORY_ROOT

    os.makedirs(MEMORY_ROOT, exist_ok=True)
    sentinel = os.path.join(MEMORY_ROOT, SENTINEL_FILENAME)
    if os.path.exists(sentinel):
        return {"status": "skipped_sentinel", "profiles": 0, "events": 0, "skipped": 0}

    db_path = _resolve_legacy_db_path(legacy_db_path)
    if not db_path:
        # Mark as migrated even when there's nothing to do — saves us from
        # rescanning every startup.
        try:
            with open(sentinel, "w") as f:
                f.write("no legacy db found\n")
        except OSError:
            pass
        return {"status": "no_legacy_db", "profiles": 0, "events": 0, "skipped": 0}

    logger.info(f"Migrating legacy memory DB: {db_path}")

    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            stats = await _migrate_rows(conn, manager)
        finally:
            conn.close()
    except sqlite3.Error as e:
        logger.error(f"Failed to open legacy db: {e}")
        return {"status": "error", "error": str(e), "profiles": 0, "events": 0, "skipped": 0}

    # Backup + sentinel only after a clean run.
    try:
        backup = db_path + LEGACY_BACKUP_SUFFIX
        # Don't clobber an existing backup; append a counter if needed.
        counter = 1
        final_backup = backup
        while os.path.exists(final_backup):
            final_backup = f"{backup}.{counter}"
            counter += 1
        os.rename(db_path, final_backup)
        logger.info(f"Legacy db backed up to {final_backup}")
    except OSError as e:
        logger.warning(f"Could not rename legacy db: {e}")

    try:
        with open(sentinel, "w") as f:
            f.write(f"migrated from {db_path}\n")
    except OSError as e:
        logger.warning(f"Could not write sentinel: {e}")

    stats["status"] = "migrated"
    return stats


async def _migrate_rows(conn: sqlite3.Connection, manager: MemoryManager) -> dict:
    profiles_migrated = 0
    events_migrated = 0
    skipped = 0

    # ── user_profiles → facts ─────────────────────────────────────────────
    try:
        rows = conn.execute(
            "SELECT user_id, memory_key, memory_value, updated_at, "
            "       confidence, category, expires_at FROM user_profiles"
        ).fetchall()
    except sqlite3.OperationalError as e:
        logger.warning(f"user_profiles read error: {e}")
        rows = []

    used_ids: dict[str, set[str]] = {}
    for row in rows:
        user_id = (row["user_id"] or "").strip()
        key = (row["memory_key"] or "").strip()
        value = row["memory_value"]
        if not user_id or not key or value is None:
            skipped += 1
            continue

        category = (row["category"] or "other").strip() or "other"
        try:
            confidence = float(row["confidence"]) if row["confidence"] is not None else 0.5
        except (TypeError, ValueError):
            confidence = 0.5
        confidence = max(0.0, min(1.0, confidence))

        base_imp = CATEGORY_IMPORTANCE.get(category, 4)
        importance = max(1, min(10, base_imp + round(confidence * 2)))

        tags = [category]
        if row["expires_at"] is not None:
            tags.append("ttl")
        tags.append("legacy_profile")

        text = f"{key}: {value}"
        # Slug — disambiguate against per-user collisions.
        slug = _slugify(key, fallback_hash=_short_hash(text))
        per_user = used_ids.setdefault(user_id, set())
        unique_slug = slug
        suffix = 1
        while unique_slug in per_user:
            unique_slug = f"{slug}_{suffix}"
            suffix += 1
        per_user.add(unique_slug)

        # tomli_w refuses to serialise ``None`` — strip any nullable fields
        # before they reach the writer.
        legacy_meta: dict = {
            "key": key,
            "category": category,
            "confidence": confidence,
        }
        if row["updated_at"] is not None:
            legacy_meta["updated_at"] = row["updated_at"]
        if row["expires_at"] is not None:
            legacy_meta["expires_at"] = int(row["expires_at"])
        source = {
            "legacy": legacy_meta,
            "migrated_from": "kiraos_legacy_db",
        }

        try:
            await manager.tree_store.add_memory(
                content_text=text,
                memory_type="fact",
                importance=importance,
                tags=tags,
                source=source,
                semantic_id=unique_slug,
                entity_id=user_id,
                entity_type="user",
                folder="facts",
            )
            profiles_migrated += 1
        except Exception as e:
            logger.warning(
                f"Failed to migrate profile {user_id}/{key}: {e}"
            )
            skipped += 1

    # ── event_logs → facts (tagged 'event') ───────────────────────────────
    try:
        rows = conn.execute(
            "SELECT id, user_id, event_summary, created_at, tag FROM event_logs"
        ).fetchall()
    except sqlite3.OperationalError as e:
        logger.warning(f"event_logs read error: {e}")
        rows = []

    for row in rows:
        user_id = (row["user_id"] or "").strip()
        summary = row["event_summary"]
        if not user_id or not summary:
            skipped += 1
            continue

        tag = (row["tag"] or "").strip()
        tags = [tag] if tag else ["event"]
        if "event" not in tags:
            tags.append("event")
        tags.append("legacy_event")

        created_at = row["created_at"] or ""
        date_part = (created_at[:10] if isinstance(created_at, str) else "") or "unknown"
        semantic_id = f"event_{row['id']}_{date_part}".replace("-", "")

        legacy_meta = {"event_id": row["id"]}
        if created_at:
            legacy_meta["created_at"] = created_at
        if tag:
            legacy_meta["tag"] = tag
        source = {
            "legacy": legacy_meta,
            "migrated_from": "kiraos_legacy_db",
        }

        try:
            await manager.tree_store.add_memory(
                content_text=summary,
                memory_type="fact",
                importance=5,
                tags=tags,
                source=source,
                semantic_id=semantic_id,
                entity_id=user_id,
                entity_type="user",
                folder="facts",
            )
            events_migrated += 1
        except Exception as e:
            logger.warning(
                f"Failed to migrate event {row['id']} for {user_id}: {e}"
            )
            skipped += 1

    return {
        "profiles": profiles_migrated,
        "events": events_migrated,
        "skipped": skipped,
    }


# Synchronous convenience entry point for callers that aren't already in an
# event loop (tests, ad-hoc scripts).
def migrate_legacy_db_sync(manager: MemoryManager, legacy_db_path: str) -> dict:
    return asyncio.run(migrate_legacy_db_if_needed(manager, legacy_db_path))
