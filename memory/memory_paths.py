"""
KiraOS_Plugin 记忆系统路径管理中枢

统一管理记忆数据根目录下所有目录结构的创建与路径解析。
支持四种实体域：user, group, channel, global

与 lightning 原版的差别：数据根目录由插件运行时通过 `set_data_root()` 注入，
不再硬编码为 `data/memory`。
"""

import os
import re
from pathlib import Path

from core.logging_manager import get_logger

logger = get_logger("kiraos_memory_paths", "green")

# ========== 实体类型 ==========
ENTITY_USER = "user"
ENTITY_GROUP = "group"
ENTITY_CHANNEL = "channel"
VALID_ENTITY_TYPES = {ENTITY_USER, ENTITY_GROUP, ENTITY_CHANNEL}

# ========== 记忆子目录 ==========
MEMORY_FOLDERS = ("facts", "reflections", "skills")

# ========== ID 安全校验 ==========
_SAFE_ID_RE = re.compile(r"^[\w\-.:]+$")


# ========== 数据根目录（运行时注入） ==========
# 由 set_data_root() 在插件初始化时设置；默认占位值仅用于本模块导入时保持可用。
_DATA_ROOT: Path = Path("data") / "memory"


def set_data_root(path) -> None:
    """注入记忆数据根目录（必须在任何写入/读取操作前调用）

    插件 initialize() 中应调用：
        set_data_root(get_data_path() / "memory")
    """
    global _DATA_ROOT
    _DATA_ROOT = Path(path)
    logger.info(f"Memory data root set to {_DATA_ROOT}")


def get_memory_root() -> str:
    return str(_DATA_ROOT)


def get_global_dir() -> str:
    return str(_DATA_ROOT / "global")


def get_entities_dir() -> str:
    return str(_DATA_ROOT / "entities")


def get_archive_dir() -> str:
    return str(_DATA_ROOT / "archive")


def get_index_db_path() -> str:
    """SQLite 索引文件位置：<data_root>/memory_index.db"""
    return str(_DATA_ROOT / "memory_index.db")


def get_chat_memory_path() -> str:
    """短期对话历史（JSON）位置：<data_root>/chat_memory.json"""
    return str(_DATA_ROOT / "chat_memory.json")


def _validate_id(entity_id: str) -> str:
    """校验实体 ID，防止路径穿越"""
    if not entity_id or not _SAFE_ID_RE.match(entity_id):
        raise ValueError(f"不合法的实体 ID: {entity_id!r}")
    return entity_id


# ========== 实体路径 ==========

def get_entity_dir(entity_id: str, entity_type: str) -> str:
    """获取实体根目录: <data_root>/entities/{type}_{id}/"""
    if entity_type not in VALID_ENTITY_TYPES:
        raise ValueError(f"未知实体类型: {entity_type!r}, 可选: {VALID_ENTITY_TYPES}")
    _validate_id(entity_id)
    return os.path.join(get_entities_dir(), f"{entity_type}_{entity_id}")


def get_entity_folder(entity_id: str, entity_type: str, folder: str) -> str:
    """获取实体下的子目录: <data_root>/entities/{type}_{id}/{folder}/"""
    return os.path.join(get_entity_dir(entity_id, entity_type), folder)


def get_entity_profile_path(entity_id: str, entity_type: str) -> str:
    """获取实体画像文件路径: <data_root>/entities/{type}_{id}/profile.json"""
    return os.path.join(get_entity_dir(entity_id, entity_type), "profile.json")


# ========== 全局路径 ==========

def get_global_self_dir() -> str:
    return os.path.join(get_global_dir(), "self")


def get_global_facts_dir() -> str:
    return os.path.join(get_global_dir(), "facts")


def get_global_skills_dir() -> str:
    return os.path.join(get_global_dir(), "skills")


# ========== 快捷方式（最常用） ==========

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


# ========== 目录初始化 ==========

def ensure_directory_structure():
    """创建完整的记忆目录骨架（启动时调用一次）"""
    global_dir = get_global_dir()
    dirs_to_create = [
        get_memory_root(),
        get_entities_dir(),
        get_archive_dir(),
        # global
        global_dir,
        os.path.join(global_dir, "facts"),
        os.path.join(global_dir, "skills"),
        os.path.join(global_dir, "self"),
        os.path.join(global_dir, "self", "facts"),
        os.path.join(global_dir, "self", "reflections"),
    ]
    for d in dirs_to_create:
        os.makedirs(d, exist_ok=True)

    logger.info("Memory directory structure initialized")


def ensure_entity_dirs(entity_id: str, entity_type: str):
    """为特定实体创建子目录（懒创建，首次写入时调用）"""
    base = get_entity_dir(entity_id, entity_type)
    os.makedirs(base, exist_ok=True)

    # 不同实体类型有不同的子目录集合
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


# ========== 扫描工具 ==========

def list_all_entities(entity_type: str = None) -> list[tuple[str, str]]:
    """扫描 entities/ 目录，返回所有 (entity_id, entity_type) 对

    Args:
        entity_type: 可选过滤，只返回指定类型的实体
    """
    results = []
    entities_dir = get_entities_dir()
    if not os.path.exists(entities_dir):
        return results

    for dirname in os.listdir(entities_dir):
        # 格式: {type}_{id}
        for et in VALID_ENTITY_TYPES:
            prefix = f"{et}_"
            if dirname.startswith(prefix):
                eid = dirname[len(prefix):]
                if entity_type is None or et == entity_type:
                    results.append((eid, et))
                break

    return results
