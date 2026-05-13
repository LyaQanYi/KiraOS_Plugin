"""
KiraOS_Plugin 旧 SQLite 记忆数据 → 新 TOML 双脑结构 迁移脚本

旧 schema（v2.x）:
    user_profiles(user_id, memory_key, memory_value, updated_at, confidence, category, expires_at)
    event_logs(id, user_id, event_summary, created_at, tag)

新 schema（v3.0）:
    <data_root>/entities/user_{user_id}/profile.json   # 用户画像
    <data_root>/entities/user_{user_id}/facts/*.toml   # 事件 → fact

幂等：靠 `<data_root>/.migrated_v3` 标记文件保证只运行一次。
原始 kiraos.db 文件被重命名为 kiraos.db.bak_<timestamp> 备份。
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from core.logging_manager import get_logger

from .toml_tree_store import TomlTreeStore
from .entity_profile import EntityProfileStore, EntityProfile
from .memory_paths import ENTITY_USER, ensure_entity_dirs, _id_to_path_segment

logger = get_logger("kiraos_memory_migrations", "green")

# 旧 schema 字段名 → EntityProfile 字段名（精确匹配）
_PROFILE_FIELD_KEYS = {
    "name": "name",
    "姓名": "name",
    "真名": "name",
    "nickname": "nickname",
    "昵称": "nickname",
    "platform": "platform",
    "平台": "platform",
    "description": "description",
    "描述": "description",
    "简介": "description",
}


async def migrate_legacy_db_if_needed(
    legacy_db_path: Path,
    data_root: Path,
) -> bool:
    """检测并执行一次性旧库迁移。

    Returns:
        True 如果实际执行了迁移，False 如果跳过（无旧库 / 已迁移过）。
    """
    legacy_db_path = Path(legacy_db_path)
    data_root = Path(data_root)
    marker = data_root / ".migrated_v3"

    if marker.exists():
        return False
    if not legacy_db_path.exists():
        return False
    # 旧文件存在但表不存在时，也跳过（说明是 v3 重建的空库）
    if not _has_legacy_tables(legacy_db_path):
        logger.info(f"Legacy db at {legacy_db_path} has no v2 tables — skip migration")
        marker.touch()
        return False

    logger.info(f"Starting legacy memory migration from {legacy_db_path}")
    counts = await asyncio.to_thread(_run_blocking_migration, legacy_db_path, data_root)

    # 备份原始旧库
    ts = int(time.time())
    backup_path = legacy_db_path.with_name(f"{legacy_db_path.name}.bak_{ts}")
    try:
        legacy_db_path.rename(backup_path)
        logger.info(f"Legacy db backed up to {backup_path}")
    except OSError as e:
        # 重命名失败不致命：标记仍写入，避免下次重复尝试；用户可手动清理
        logger.warning(f"Could not rename legacy db: {e}")

    marker.touch()
    logger.info(
        f"Migration complete: {counts['profiles']} profile users, "
        f"{counts['events']} event rows → TOML"
    )
    return True


def _has_legacy_tables(db_path: Path) -> bool:
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            cur = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('user_profiles','event_logs')"
            )
            tables = {row[0] for row in cur.fetchall()}
            return "user_profiles" in tables or "event_logs" in tables
        finally:
            conn.close()
    except sqlite3.DatabaseError:
        return False


def _run_blocking_migration(legacy_db_path: Path, data_root: Path) -> dict:
    """同步执行迁移逻辑（在 to_thread 中调用）"""
    now_epoch = int(time.time())
    counts = {"profiles": 0, "events": 0, "skipped_expired": 0}

    conn = sqlite3.connect(f"file:{legacy_db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        # ── 1. 迁移 user_profiles → EntityProfile ─────────────────
        # 前置 _has_legacy_tables 检查只要求 user_profiles / event_logs 任一存在
        # 就触发迁移，所以这里要兜底缺表的情况——和下面 event_logs 的处理对称。
        profiles_by_user: dict[str, EntityProfile] = {}
        try:
            cur = conn.execute(
                "SELECT user_id, memory_key, memory_value, confidence, category, expires_at "
                "FROM user_profiles ORDER BY user_id, memory_key"
            )
        except sqlite3.OperationalError:
            cur = []
        for row in cur:
            user_id = row["user_id"]
            if not user_id:
                continue
            # 跳过已过期
            exp = row["expires_at"]
            if exp is not None and exp != "" and int(exp) <= now_epoch:
                counts["skipped_expired"] += 1
                continue

            if user_id not in profiles_by_user:
                profiles_by_user[user_id] = EntityProfile(
                    entity_id=user_id, entity_type=ENTITY_USER
                )
            profile = profiles_by_user[user_id]
            _apply_legacy_row_to_profile(profile, row)

        # 落盘 profiles
        for user_id, profile in profiles_by_user.items():
            ensure_entity_dirs(user_id, ENTITY_USER)
            _sync_save_profile(profile, data_root)
            counts["profiles"] += 1

            if counts["profiles"] % 50 == 0:
                logger.info(f"Migrated {counts['profiles']} user profiles...")

        # ── 2. 迁移 event_logs → facts/*.toml ──────────────────────
        # 直接调用 TomlTreeStore 的同步 TOML 写函数
        try:
            cur = conn.execute(
                "SELECT id, user_id, event_summary, created_at, tag FROM event_logs ORDER BY id"
            )
        except sqlite3.OperationalError:
            cur = []

        for row in cur:
            user_id = row["user_id"]
            text = row["event_summary"]
            if not user_id or not text:
                continue

            tag = row["tag"] if "tag" in row.keys() else None
            tags = [tag] if tag else []
            source = {
                "legacy_event_id": int(row["id"]),
                "time": _iso_from_legacy_created_at(row["created_at"]),
            }
            mem_id = f"event_{int(row['id'])}"

            ensure_entity_dirs(user_id, ENTITY_USER)
            _write_fact_toml(
                data_root=data_root,
                entity_id=user_id,
                entity_type=ENTITY_USER,
                mem_id=mem_id,
                text=text,
                tags=tags,
                source=source,
            )
            counts["events"] += 1

            if counts["events"] % 200 == 0:
                logger.info(f"Migrated {counts['events']} event rows...")
    finally:
        conn.close()

    return counts


def _apply_legacy_row_to_profile(profile: EntityProfile, row) -> None:
    """把旧 user_profiles 一行映射到 EntityProfile 字段"""
    key = (row["memory_key"] or "").strip()
    value = (row["memory_value"] or "").strip()
    if not key or not value:
        return

    category = (row["category"] or "basic").strip()

    # 1. 精确字段映射（覆盖 name/nickname/platform/description）
    mapped = _PROFILE_FIELD_KEYS.get(key)
    if mapped:
        setattr(profile, mapped, value)
        return

    # 2. 按 category 分桶
    if category == "preference":
        profile.preferences[key] = value
    elif category == "social":
        profile.relationships[key] = value
    elif category == "basic":
        # 基础信息：作为特征条目（用 "key: value" 形式保留可读性）
        trait = f"{key}: {value}" if key else value
        if trait not in profile.traits:
            profile.traits.append(trait)
    else:
        # other / 未知 → fact
        fact = f"{key}: {value}"
        if fact not in profile.facts:
            profile.facts.append(fact)


def _sync_save_profile(profile: EntityProfile, data_root: Path) -> None:
    """直接同步写 profile.json（不走 EntityProfileStore 异步路径）"""
    import json

    target_dir = data_root / "entities" / f"{profile.entity_type}_{_id_to_path_segment(profile.entity_id)}"
    target_dir.mkdir(parents=True, exist_ok=True)
    fpath = target_dir / "profile.json"
    with open(fpath, "w", encoding="utf-8") as f:
        json.dump(profile.to_dict(), f, ensure_ascii=False, indent=2)


def _write_fact_toml(
    *,
    data_root: Path,
    entity_id: str,
    entity_type: str,
    mem_id: str,
    text: str,
    tags: list,
    source: dict,
) -> None:
    """同步写一条 fact 到 entities/{type}_{id}/facts/{mem_id}.toml"""
    import tomli_w

    facts_dir = data_root / "entities" / f"{entity_type}_{_id_to_path_segment(entity_id)}" / "facts"
    facts_dir.mkdir(parents=True, exist_ok=True)

    fpath = facts_dir / f"{mem_id}.toml"
    if fpath.exists():
        # 已存在同名文件（重跑场景）：跳过，避免覆盖用户后续手动改动
        return

    doc = {
        "id": mem_id,
        "type": "fact",
        "text": text,
        "importance": 5,
        "tags": tags,
        "source": source,
    }
    with open(fpath, "wb") as f:
        tomli_w.dump(doc, f)


def _iso_from_legacy_created_at(value) -> str:
    """旧 created_at 字段（SQLite CURRENT_TIMESTAMP，ISO 字符串）转回 ISO with TZ"""
    if not value:
        return datetime.now(timezone.utc).isoformat()
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()
    s = str(value).strip()
    if not s:
        return datetime.now(timezone.utc).isoformat()
    # SQLite CURRENT_TIMESTAMP format: 'YYYY-MM-DD HH:MM:SS' (UTC, no TZ)
    try:
        dt = datetime.fromisoformat(s.replace(" ", "T"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.isoformat()
    except ValueError:
        return datetime.now(timezone.utc).isoformat()
