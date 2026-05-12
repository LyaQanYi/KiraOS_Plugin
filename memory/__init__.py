"""
KiraOS_Plugin 记忆子系统

双脑双存储记忆系统，从 KiraAI-lightning 移植：
- 快系统：SQLite + FTS5 全文检索（jieba 中文分词）
- 慢系统：海马体后台异步（提取/反思/画像）
- 真相源：TOML 文件
- 实体维度：user / group / channel / global

主要导出：
    from .memory import MemoryManager
    from .memory.memory_paths import set_data_root
    from .memory.migrations import migrate_legacy_db_if_needed
"""

from .memory_manager import MemoryManager
from .toml_tree_store import TomlTreeStore, Memory
from .entity_profile import EntityProfileStore, EntityProfile
from .memory_index import MemoryIndex
from .memory_paths import (
    set_data_root,
    get_memory_root,
    get_entities_dir,
    get_global_dir,
    get_archive_dir,
    list_all_entities,
    ensure_directory_structure,
    ENTITY_USER,
    ENTITY_GROUP,
    ENTITY_CHANNEL,
)

__all__ = [
    "MemoryManager",
    "TomlTreeStore",
    "Memory",
    "EntityProfileStore",
    "EntityProfile",
    "MemoryIndex",
    "set_data_root",
    "get_memory_root",
    "get_entities_dir",
    "get_global_dir",
    "get_archive_dir",
    "list_all_entities",
    "ensure_directory_structure",
    "ENTITY_USER",
    "ENTITY_GROUP",
    "ENTITY_CHANNEL",
]
