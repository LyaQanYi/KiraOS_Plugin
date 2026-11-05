"""Merge user data written under identity aliases into canonical entities.

identity.py makes new reads/writes resolve to the canonical user entity, but
data created before the alias mapping existed still lives in
entities/user_{alias}/. This module folds those directories into
entities/user_{canonical}/ at plugin startup, before the SQLite index is
rebuilt from TOML:

- profiles: union fields/facts/traits/aliases, sum interaction counters, max
  last_interaction; the canonical profile wins on conflicting scalar fields.
- memory TOMLs: move into the canonical facts/reflections folders. An
  identical text is de-duplicated; a different text with the same filename is
  renamed with a content-hash suffix so nothing is overwritten.
- empty alias directories are removed. The operation is idempotent: once the
  alias directory is gone there is nothing left to move.

Only user entities are touched. Group/channel data is never merged.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Dict, Optional, Tuple

try:
    import tomllib  # Python 3.11+
except ImportError:  # pragma: no cover - Python 3.10
    import tomli as tomllib  # type: ignore

from core.logging_manager import get_logger

from .entity_profile import EntityProfile, EntityProfileStore
from .identity import resolved_aliases
from .memory_paths import (
    ENTITY_USER,
    get_entity_dir,
    get_entity_profile_path,
)

logger = get_logger("kiraos_identity_migration", "cyan")

_FOLDERS = ("facts", "reflections")


def _sha8(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]


def _read_profile(path: Path) -> Tuple[Optional[EntityProfile], bool]:
    """Return (profile, valid). Missing file is valid/None; malformed is invalid."""
    if not path.is_file():
        return None, True
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("identity merge: unreadable profile %s: %s", path, exc)
        return None, False
    if not isinstance(data, dict):
        logger.warning("identity merge: profile %s is not a JSON object", path)
        return None, False
    try:
        return EntityProfile.from_dict(data), True
    except Exception as exc:
        logger.warning("identity merge: invalid profile %s: %s", path, exc)
        return None, False


def _merge_profile(alias_profile: EntityProfile, canonical: EntityProfile) -> bool:
    changed = False
    for field_name in ("name", "nickname", "description", "platform"):
        value = getattr(alias_profile, field_name, "") or ""
        if value and not getattr(canonical, field_name, ""):
            setattr(canonical, field_name, value)
            changed = True
    for field_name in ("traits", "aliases"):
        current = list(getattr(canonical, field_name, []) or [])
        for value in getattr(alias_profile, field_name, []) or []:
            if value and value not in current:
                current.append(value)
                changed = True
        setattr(canonical, field_name, current)
    for value in (alias_profile.name, alias_profile.nickname):
        if value and value not in canonical.aliases and value not in (
            canonical.name,
            canonical.nickname,
        ):
            canonical.aliases.append(value)
            changed = True
    merged_facts = list(canonical.facts or [])
    for value in alias_profile.facts or []:
        if value and value not in merged_facts:
            merged_facts.append(value)
            changed = True
    canonical.facts = merged_facts
    for field_name in ("preferences", "relationships"):
        current = getattr(canonical, field_name, {}) or {}
        if not isinstance(current, dict):
            current = {}
        incoming = getattr(alias_profile, field_name, {}) or {}
        if isinstance(incoming, dict):
            for key, value in incoming.items():
                if key not in current:
                    current[key] = value
                    changed = True
            setattr(canonical, field_name, current)
    metadata = canonical.metadata if isinstance(canonical.metadata, dict) else {}
    incoming_meta = alias_profile.metadata if isinstance(alias_profile.metadata, dict) else {}
    for key, value in incoming_meta.items():
        if key not in metadata:
            metadata[key] = value
            changed = True
    canonical.metadata = metadata
    count = int(alias_profile.interaction_count or 0) + int(canonical.interaction_count or 0)
    if count != (canonical.interaction_count or 0):
        canonical.interaction_count = count
        changed = True
    last = max(float(canonical.last_interaction or 0), float(alias_profile.last_interaction or 0))
    if last != (canonical.last_interaction or 0):
        canonical.last_interaction = last
        changed = True
    return changed


def _toml_text(path: Path) -> Optional[str]:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, tomllib.TOMLDecodeError):
        return None
    text = data.get("text")
    return text if isinstance(text, str) else None


def _move_file(src: Path, dst: Path, dry_run: bool, stats: Dict[str, int]) -> None:
    if not dst.exists():
        if dry_run:
            stats["files_moved"] += 1
            return
        os.replace(src, dst)
        stats["files_moved"] += 1
        return
    src_text = _toml_text(src)
    dst_text = _toml_text(dst)
    if src_text is not None and src_text == dst_text:
        if not dry_run:
            src.unlink()
        stats["duplicates_removed"] += 1
        return
    suffix = _sha8(src_text if src_text is not None else src.name)
    candidate = dst.with_name(f"{dst.stem}_{suffix}.toml")
    attempt = 2
    while candidate.exists():
        if _toml_text(candidate) == src_text and src_text is not None:
            if not dry_run:
                src.unlink()
            stats["duplicates_removed"] += 1
            return
        candidate = dst.with_name(f"{dst.stem}_{suffix}_{attempt}.toml")
        attempt += 1
    if dry_run:
        stats["files_moved"] += 1
        return
    os.replace(src, candidate)
    stats["files_moved"] += 1


async def reconcile_identity_aliases(
    profile_store: Optional[EntityProfileStore], *, dry_run: bool = False
) -> Dict[str, int]:
    """Move alias user entities into their canonical entity; returns counts."""
    stats: Dict[str, int] = {
        "aliases": 0,
        "profiles_merged": 0,
        "files_moved": 0,
        "duplicates_removed": 0,
        "skipped": 0,
        "errors": 0,
    }
    aliases = resolved_aliases()
    if not aliases:
        return stats

    for alias, canonical in sorted(aliases.items()):
        try:
            alias_dir = Path(get_entity_dir(alias, ENTITY_USER))
            canonical_dir = Path(get_entity_dir(canonical, ENTITY_USER))
        except ValueError as exc:
            logger.warning(
                "identity merge: invalid entity id %s -> %s: %s", alias, canonical, exc
            )
            stats["errors"] += 1
            continue
        if alias_dir == canonical_dir or not alias_dir.is_dir():
            continue
        stats["aliases"] += 1

        alias_profile_path = Path(get_entity_profile_path(alias, ENTITY_USER))
        canonical_profile_path = Path(get_entity_profile_path(canonical, ENTITY_USER))
        alias_profile, alias_valid = _read_profile(alias_profile_path)
        if not alias_valid:
            stats["skipped"] += 1
            logger.warning(
                "identity merge: leaving %s untouched (unreadable profile)", alias_dir
            )
            continue

        canonical_profile: Optional[EntityProfile] = None
        if canonical_profile_path.exists():
            canonical_profile, canonical_valid = _read_profile(canonical_profile_path)
            if not canonical_valid:
                stats["skipped"] += 1
                logger.warning(
                    "identity merge: leaving %s untouched (canonical profile %s is invalid)",
                    alias_dir,
                    canonical_profile_path,
                )
                continue

        if alias_profile is not None:
            if dry_run:
                stats["profiles_merged"] += 1
            else:
                if profile_store is None:
                    raise ValueError("profile_store is required for a real identity merge")
                if canonical_profile is None:
                    canonical_profile = await profile_store.get_profile(
                        canonical, ENTITY_USER
                    )
                mutable = EntityProfile.from_dict(canonical_profile.to_dict())
                _merge_profile(alias_profile, mutable)
                mutable.entity_id = canonical
                mutable.entity_type = ENTITY_USER
                if not await profile_store.save_profile(mutable):
                    raise RuntimeError(f"could not save merged profile {canonical}")
                stats["profiles_merged"] += 1
                try:
                    alias_profile_path.unlink()
                except OSError:
                    pass

        for folder in _FOLDERS:
            src_dir = alias_dir / folder
            if not src_dir.is_dir():
                continue
            dst_dir = canonical_dir / folder
            if not dry_run:
                dst_dir.mkdir(parents=True, exist_ok=True)
            for src in sorted(src_dir.glob("*.toml")):
                try:
                    _move_file(src, dst_dir / src.name, dry_run, stats)
                except OSError as exc:
                    stats["errors"] += 1
                    logger.warning("identity merge: failed to move %s: %s", src, exc)
            if not dry_run:
                try:
                    src_dir.rmdir()
                except OSError:
                    pass

        if not dry_run:
            try:
                alias_dir.rmdir()
            except OSError:
                logger.debug(
                    "identity merge: %s still has files after merge; leaving it", alias_dir
                )

    logger.info(
        "Identity alias reconcile: aliases=%d profiles=%d files_moved=%d duplicates=%d skipped=%d errors=%d",
        stats["aliases"],
        stats["profiles_merged"],
        stats["files_moved"],
        stats["duplicates_removed"],
        stats["skipped"],
        stats["errors"],
    )
    return stats
