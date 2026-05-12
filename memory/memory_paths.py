"""
Memory subsystem path management.

Owns the layout under ``<data_dir>/memory/``:

    memory/
    ├── entities/{user|group|channel}_<id>/
    │   ├── profile.json
    │   ├── facts/*.toml
    │   └── reflections/*.toml
    ├── global/
    │   ├── facts/*.toml
    │   ├── skills/*.toml
    │   └── self/
    │       ├── facts/*.toml
    │       └── reflections/*.toml
    └── archive/*.toml

Module-level path constants are evaluated on import from
``core.utils.path_utils.get_data_path()``. Tests can monkey-patch these
constants directly (see ``test_memory_system.setup_test_env``).
"""

import os
import re

from core.logging_manager import get_logger
from core.utils.path_utils import get_data_path

logger = get_logger("kiraos_memory_paths", "green")

# ========== Root paths ==========
MEMORY_ROOT = str(get_data_path() / "memory")
GLOBAL_DIR = os.path.join(MEMORY_ROOT, "global")
ENTITIES_DIR = os.path.join(MEMORY_ROOT, "entities")
ARCHIVE_DIR = os.path.join(MEMORY_ROOT, "archive")

# ========== Entity types ==========
ENTITY_USER = "user"
ENTITY_GROUP = "group"
ENTITY_CHANNEL = "channel"
VALID_ENTITY_TYPES = {ENTITY_USER, ENTITY_GROUP, ENTITY_CHANNEL}

# ========== Memory subfolders ==========
MEMORY_FOLDERS = ("facts", "reflections", "skills")

# ========== ID validation (path-traversal guard) ==========
_SAFE_ID_RE = re.compile(r"^[\w\-.:]+$")


def _validate_id(entity_id: str) -> str:
    if not entity_id or not _SAFE_ID_RE.match(entity_id):
        raise ValueError(f"Invalid entity id: {entity_id!r}")
    return entity_id


# ========== Entity paths ==========

def get_entity_dir(entity_id: str, entity_type: str) -> str:
    if entity_type not in VALID_ENTITY_TYPES:
        raise ValueError(f"Unknown entity_type: {entity_type!r}, expected one of {VALID_ENTITY_TYPES}")
    _validate_id(entity_id)
    return os.path.join(ENTITIES_DIR, f"{entity_type}_{entity_id}")


def get_entity_folder(entity_id: str, entity_type: str, folder: str) -> str:
    return os.path.join(get_entity_dir(entity_id, entity_type), folder)


def get_entity_profile_path(entity_id: str, entity_type: str) -> str:
    return os.path.join(get_entity_dir(entity_id, entity_type), "profile.json")


# ========== Global paths ==========

def get_global_dir() -> str:
    return GLOBAL_DIR


def get_global_self_dir() -> str:
    return os.path.join(GLOBAL_DIR, "self")


def get_global_facts_dir() -> str:
    return os.path.join(GLOBAL_DIR, "facts")


def get_global_skills_dir() -> str:
    return os.path.join(GLOBAL_DIR, "skills")


# ========== Archive ==========

def get_archive_dir() -> str:
    return ARCHIVE_DIR


# ========== Shortcuts ==========

def get_user_dir(user_id: str) -> str:
    return get_entity_dir(user_id, ENTITY_USER)


def get_user_folder(user_id: str, folder: str) -> str:
    return get_entity_folder(user_id, ENTITY_USER, folder)


def get_group_dir(group_id: str) -> str:
    return get_entity_dir(group_id, ENTITY_GROUP)


def get_group_folder(group_id: str, folder: str) -> str:
    return get_entity_folder(group_id, ENTITY_GROUP, folder)


def get_channel_dir(channel_id: str) -> str:
    return get_entity_dir(channel_id, ENTITY_CHANNEL)


def get_channel_folder(channel_id: str, folder: str) -> str:
    return get_entity_folder(channel_id, ENTITY_CHANNEL, folder)


# ========== Initialization ==========

def ensure_directory_structure():
    """Create the full memory directory skeleton. Call once at startup."""
    dirs_to_create = [
        MEMORY_ROOT,
        ENTITIES_DIR,
        ARCHIVE_DIR,
        GLOBAL_DIR,
        os.path.join(GLOBAL_DIR, "facts"),
        os.path.join(GLOBAL_DIR, "skills"),
        os.path.join(GLOBAL_DIR, "self"),
        os.path.join(GLOBAL_DIR, "self", "facts"),
        os.path.join(GLOBAL_DIR, "self", "reflections"),
    ]
    for d in dirs_to_create:
        os.makedirs(d, exist_ok=True)

    logger.info("Memory directory structure initialized")


def ensure_entity_dirs(entity_id: str, entity_type: str):
    """Lazy-create per-entity subfolders on first write."""
    base = get_entity_dir(entity_id, entity_type)
    os.makedirs(base, exist_ok=True)

    if entity_type == ENTITY_USER:
        folders = ("facts", "reflections")
    elif entity_type == ENTITY_GROUP:
        folders = ("facts", "reflections")
    elif entity_type == ENTITY_CHANNEL:
        folders = ("facts",)
    else:
        folders = ("facts",)

    for folder in folders:
        os.makedirs(os.path.join(base, folder), exist_ok=True)


# ========== Scanning ==========

def list_all_entities(entity_type: str = None) -> list[tuple[str, str]]:
    """Scan entities/ and return all (entity_id, entity_type) pairs."""
    results = []
    if not os.path.exists(ENTITIES_DIR):
        return results

    for dirname in os.listdir(ENTITIES_DIR):
        for et in VALID_ENTITY_TYPES:
            prefix = f"{et}_"
            if dirname.startswith(prefix):
                eid = dirname[len(prefix):]
                if entity_type is None or et == entity_type:
                    results.append((eid, et))
                break

    return results
