"""
实体画像存储层

支持 user / group / channel 三种实体类型。
画像以 profile.json 存储，完全取代旧的 profile.md + YAML frontmatter 方案。
"""

import json
import os
import time
import asyncio
from dataclasses import dataclass, field, fields, asdict
from typing import Optional

from core.logging_manager import get_logger
from .memory_paths import (
    get_entity_profile_path,
    ensure_entity_dirs,
    ENTITY_USER,
    ENTITY_GROUP,
    ENTITY_CHANNEL,
)

logger = get_logger("entity_profile", "green")


@dataclass
class EntityProfile:
    """通用实体画像数据类

    适用于 user、group、channel 三种实体。
    序列化为 profile.json。
    """

    entity_id: str
    entity_type: str = ENTITY_USER  # user / group / channel

    name: str = ""
    nickname: str = ""
    description: str = ""
    platform: str = ""

    # 特征标签（["耐心", "技术导向", ...]）
    traits: list = field(default_factory=list)
    # 偏好字典（{"theme": "dark", "language": "zh"}）
    preferences: dict = field(default_factory=dict)
    # 关系图（{"user_456": "好友", "group_123": "管理员"}）
    relationships: dict = field(default_factory=dict)
    # 已知核心事实（高度浓缩的关键信息）
    facts: list = field(default_factory=list)
    # 曾用名/历史昵称（用于昵称反查，自动维护）
    aliases: list = field(default_factory=list)

    interaction_count: int = 0
    last_interaction: float = 0.0

    # 自由扩展区
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        """序列化为字典"""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "EntityProfile":
        """从字典反序列化（忽略多余字段）"""
        valid_fields = {f.name for f in fields(cls)}
        filtered = {k: v for k, v in data.items() if k in valid_fields}
        return cls(**filtered)

    def to_prompt(self) -> str:
        """格式化为 LLM Prompt 文本"""
        parts = []
        if self.name:
            parts.append(f"名字: {self.name}")
        if self.nickname and self.nickname != self.name:
            parts.append(f"昵称: {self.nickname}")
        if self.aliases:
            parts.append(f"曾用名: {', '.join(self.aliases)}")
        if self.platform:
            parts.append(f"平台: {self.platform}")
        if self.description:
            parts.append(f"描述: {self.description}")
        if self.traits:
            parts.append(f"特征: {', '.join(self.traits)}")
        if self.preferences:
            prefs = ", ".join(f"{k}: {v}" for k, v in self.preferences.items())
            parts.append(f"偏好: {prefs}")
        if self.relationships:
            rels = ", ".join(f"{k}: {v}" for k, v in self.relationships.items())
            parts.append(f"关系: {rels}")
        if self.facts:
            facts_str = "\n  ".join(f"- {f}" for f in self.facts)
            parts.append(f"已知事实:\n  {facts_str}")
        if self.interaction_count:
            parts.append(f"互动次数: {self.interaction_count}")
        return "\n".join(parts) if parts else "暂无画像信息"


class EntityProfileStore:
    """实体画像 CRUD 管理器

    所有操作均为异步。画像存储为:
    data/memory/entities/{type}_{id}/profile.json
    """

    def __init__(self):
        self._lock = asyncio.Lock()
        logger.info("EntityProfileStore initialized")

    async def get_profile(
        self, entity_id: str, entity_type: str = ENTITY_USER
    ) -> EntityProfile:
        """获取实体画像，不存在则创建默认画像"""
        fpath = get_entity_profile_path(entity_id, entity_type)

        if os.path.exists(fpath):
            try:
                data = await asyncio.to_thread(self._sync_read, fpath)
                return EntityProfile.from_dict(data)
            except Exception as e:
                logger.error(f"Failed to read profile {fpath}: {e}")

        # 创建默认画像
        profile = EntityProfile(entity_id=entity_id, entity_type=entity_type)
        await self.save_profile(profile)
        return profile

    async def save_profile(self, profile: EntityProfile) -> bool:
        """保存画像到文件"""
        ensure_entity_dirs(profile.entity_id, profile.entity_type)
        fpath = get_entity_profile_path(profile.entity_id, profile.entity_type)

        async with self._lock:
            try:
                await asyncio.to_thread(self._sync_write, fpath, profile.to_dict())
                return True
            except Exception as e:
                logger.error(f"Failed to save profile {fpath}: {e}")
                return False

    async def update_profile(
        self, entity_id: str, entity_type: str = ENTITY_USER, **kwargs
    ) -> EntityProfile:
        """部分更新画像字段"""
        profile = await self.get_profile(entity_id, entity_type)
        allowed = {f.name for f in fields(EntityProfile)} - {"entity_id", "entity_type"}

        for key, value in kwargs.items():
            if key in allowed:
                setattr(profile, key, value)

        profile.last_interaction = time.time()
        await self.save_profile(profile)
        return profile

    # === 便捷方法 ===

    async def add_trait(self, entity_id: str, trait: str, entity_type: str = ENTITY_USER):
        """添加特征标签"""
        profile = await self.get_profile(entity_id, entity_type)
        if trait not in profile.traits:
            profile.traits.append(trait)
            await self.save_profile(profile)

    async def remove_trait(self, entity_id: str, trait: str, entity_type: str = ENTITY_USER):
        """移除特征标签"""
        profile = await self.get_profile(entity_id, entity_type)
        if trait in profile.traits:
            profile.traits.remove(trait)
            await self.save_profile(profile)

    async def add_fact(self, entity_id: str, fact: str, entity_type: str = ENTITY_USER):
        """添加核心事实到画像"""
        profile = await self.get_profile(entity_id, entity_type)
        if fact not in profile.facts:
            profile.facts.append(fact)
            await self.save_profile(profile)

    async def update_fact(
        self, entity_id: str, old_fact: str, new_fact: str, entity_type: str = ENTITY_USER
    ):
        """更新画像中的事实"""
        profile = await self.get_profile(entity_id, entity_type)
        if old_fact in profile.facts:
            idx = profile.facts.index(old_fact)
            profile.facts[idx] = new_fact
            await self.save_profile(profile)

    async def remove_fact(self, entity_id: str, fact: str, entity_type: str = ENTITY_USER):
        """移除画像中的事实"""
        profile = await self.get_profile(entity_id, entity_type)
        if fact in profile.facts:
            profile.facts.remove(fact)
            await self.save_profile(profile)

    async def set_relationship(
        self,
        entity_id: str,
        target: str,
        relation: str,
        entity_type: str = ENTITY_USER,
    ):
        """设置关系"""
        profile = await self.get_profile(entity_id, entity_type)
        profile.relationships[target] = relation
        await self.save_profile(profile)

    async def increment_interaction(
        self, entity_id: str, entity_type: str = ENTITY_USER, **extra_updates
    ):
        """递增交互计数并可选更新其他字段

        特别处理 nickname 变更：旧昵称自动归档到 aliases。
        """
        profile = await self.get_profile(entity_id, entity_type)
        profile.interaction_count += 1
        profile.last_interaction = time.time()

        # 昵称变更时，将旧昵称归档到 aliases
        new_nickname = extra_updates.get("nickname", "")
        if new_nickname and profile.nickname and new_nickname != profile.nickname:
            if profile.nickname not in profile.aliases:
                profile.aliases.append(profile.nickname)

        allowed = {f.name for f in fields(EntityProfile)} - {"entity_id", "entity_type"}
        for key, value in extra_updates.items():
            if key in allowed:
                setattr(profile, key, value)

        await self.save_profile(profile)

    async def resolve_entity_by_name(
        self, name_query: str, entity_type: str = ENTITY_USER
    ) -> Optional[str]:
        """通过昵称、QQ号或名字反查 entity_id

        扫描所有指定类型的实体画像，匹配优先级：
        1. entity_id 尾部精确匹配（纯数字 QQ 号）
        2. name/nickname 精确匹配
        3. name/nickname 包含匹配
        返回最佳匹配的 entity_id，找不到则返回 None。
        """
        from .memory_paths import list_all_entities

        if not name_query or not name_query.strip():
            return None

        query = name_query.strip()
        query_lower = query.lower()
        is_numeric = query.isdigit()
        candidates = []

        for eid, etype in list_all_entities(entity_type):
            # 优先级 1：entity_id 尾部匹配（adapter:qq_number 格式）
            # 用户传入纯 QQ 号 "123456789"，匹配 "onebot:123456789"
            if is_numeric and eid.endswith(f":{query}"):
                return eid

            try:
                profile = await self.get_profile(eid, etype)
            except Exception:
                continue

            # 优先级 2：name/nickname/aliases 精确匹配
            if profile.name and profile.name.lower() == query_lower:
                return eid
            if profile.nickname and profile.nickname.lower() == query_lower:
                return eid
            for alias in profile.aliases:
                if alias and alias.lower() == query_lower:
                    return eid

            # 优先级 3：包含匹配（兜底）
            if profile.name and query_lower in profile.name.lower():
                candidates.append((eid, len(profile.name)))
            elif profile.nickname and query_lower in profile.nickname.lower():
                candidates.append((eid, len(profile.nickname)))
            else:
                for alias in profile.aliases:
                    if alias and query_lower in alias.lower():
                        candidates.append((eid, len(alias)))
                        break

        # 返回名字最短的（最精确匹配）
        if candidates:
            candidates.sort(key=lambda x: x[1])
            return candidates[0][0]

        return None

    async def get_profile_prompt(
        self, entity_id: str, entity_type: str = ENTITY_USER
    ) -> str:
        """获取画像的 Prompt 格式文本"""
        profile = await self.get_profile(entity_id, entity_type)
        return profile.to_prompt()

    async def delete_profile(
        self, entity_id: str, entity_type: str = ENTITY_USER
    ) -> bool:
        """删除画像文件"""
        fpath = get_entity_profile_path(entity_id, entity_type)
        try:
            if os.path.exists(fpath):
                await asyncio.to_thread(os.remove, fpath)
                return True
        except Exception as e:
            logger.error(f"Failed to delete profile {fpath}: {e}")
        return False

    # === 内部同步 IO ===

    @staticmethod
    def _sync_read(fpath: str) -> dict:
        with open(fpath, "r", encoding="utf-8") as f:
            return json.load(f)

    @staticmethod
    def _sync_write(fpath: str, data: dict):
        os.makedirs(os.path.dirname(fpath), exist_ok=True)
        with open(fpath, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
