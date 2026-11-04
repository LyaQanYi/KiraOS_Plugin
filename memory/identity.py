"""Cross-adapter identity aliases for user entities.

KiraOS keys every memory by an adapter-scoped entity id such as
"qq:1000000001" or "webchat:admin". The same person using several adapters
would otherwise get several isolated profiles and fact sets. This module lets
the plugin config map such aliases to one canonical user entity:

    identity_aliases = [
        "webchat:admin=qq:1000000001",
        "telegram:123456=qq:1000000001",
    ]

Only user entities are resolved; group/channel ids pass through unchanged. An
empty map (the default) keeps the old adapter-isolated behavior.

The mapping is process-global because it is read from storage modules that do
not own the plugin config (TomlTreeStore, EntityProfileStore, MemoryManager,
tools, WebUI). UserMemoryPlugin.initialize() installs it once before the
memory manager is constructed. identity_migration.py then merges any existing
data that was written under an alias before the mapping existed.
"""

from __future__ import annotations

import re
from typing import Dict, Iterable, List, Optional

from core.logging_manager import get_logger

from .memory_paths import ENTITY_USER

logger = get_logger("kiraos_identity", "cyan")

# "a -> b", "a => b" and "a = b" are all accepted; the first separator wins.
_ENTRY_SPLIT = re.compile(r"s*(?:->|=>|=)s*")

_ALIASES: Dict[str, str] = {}


def _iter_entries(entries) -> Iterable[str]:
    if entries is None:
        return []
    if isinstance(entries, str):
        raw = entries.replace("\n", ",").split(",")
        return [part.strip() for part in raw if part.strip()]
    if isinstance(entries, dict):
        return [f"{key}={value}" for key, value in entries.items()]
    if isinstance(entries, (list, tuple, set)):
        out = []
        for item in entries:
            if isinstance(item, str) and item.strip():
                out.append(item.strip())
            elif isinstance(item, dict):
                for key, value in item.items():
                    out.append(f"{key}={value}")
        return out
    return []


def parse_aliases(entries) -> Dict[str, str]:
    """Parse and validate alias entries; return alias -> final canonical id.

    Chains are resolved transitively and cycles are dropped with a warning.
    Invalid entries are ignored rather than aborting plugin startup.
    """
    raw: Dict[str, str] = {}
    for entry in _iter_entries(entries):
        parts = _ENTRY_SPLIT.split(entry, maxsplit=1)
        if len(parts) != 2:
            logger.warning("ignoring identity alias without a separator: %r", entry)
            continue
        alias, canonical = parts[0].strip(), parts[1].strip()
        if not alias or not canonical:
            logger.warning("ignoring empty identity alias: %r", entry)
            continue
        if ":" not in alias or ":" not in canonical:
            logger.warning(
                "ignoring identity alias without adapter prefix: %r", entry
            )
            continue
        if alias == canonical:
            continue
        if alias in raw and raw[alias] != canonical:
            logger.warning(
                "ignoring duplicate identity alias %s (keeping %s, dropping %s)",
                alias,
                raw[alias],
                canonical,
            )
            continue
        raw[alias] = canonical

    resolved: Dict[str, str] = {}
    for alias in sorted(raw):
        chain: List[str] = []
        current: Optional[str] = alias
        while current is not None and current in raw:
            if current in chain:
                logger.warning(
                    "identity alias cycle involving %s; ignoring that chain",
                    " -> ".join(chain + [current]),
                )
                current = None
                break
            chain.append(current)
            current = raw[current]
        if current is None:
            continue
        if current != alias:
            resolved[alias] = current
    return resolved


def set_aliases(entries) -> Dict[str, str]:
    """Install the process-global alias map; returns the resolved map."""
    global _ALIASES
    _ALIASES = parse_aliases(entries)
    if _ALIASES:
        logger.info(
            "Cross-adapter identity aliases installed: %s",
            ", ".join(f"{alias}->{canonical}" for alias, canonical in sorted(_ALIASES.items())),
        )
    return dict(_ALIASES)


def get_aliases() -> Dict[str, str]:
    return dict(_ALIASES)


def resolved_aliases() -> Dict[str, str]:
    """Alias -> final canonical id, excluding identity entries."""
    return {alias: canonical for alias, canonical in _ALIASES.items() if alias != canonical}


def aliases_by_canonical() -> Dict[str, List[str]]:
    grouped: Dict[str, List[str]] = {}
    for alias, canonical in sorted(_ALIASES.items()):
        grouped.setdefault(canonical, []).append(alias)
    return grouped


def resolve_entity_id(entity_id: str, entity_type: str = ENTITY_USER) -> str:
    """Return the canonical user entity id for an alias; other types unchanged."""
    if not entity_id or not _ALIASES:
        return entity_id
    if entity_type not in (ENTITY_USER, ""):
        return entity_id
    return _ALIASES.get(entity_id, entity_id)


def is_alias(entity_id: str) -> bool:
    return bool(entity_id) and entity_id in _ALIASES


def clear_aliases() -> None:
    """Test helper: reset the process-global map."""
    global _ALIASES
    _ALIASES = {}
