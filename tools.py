"""
KiraOS_Plugin 记忆系统 LLM 工具实现

工具清单（采用 lightning 新接口）：
- memory_add      — 写入新记忆（含两级去重）
- memory_search   — 语义检索
- memory_update_entry — 更新已有记忆
- memory_remove   — 移入归档
- profile_view    — 查看用户画像
- profile_update  — 更新画像（trait/fact/relationship）

本模块只暴露纯异步函数，由 main.py 用 @register_tool 装饰器套壳后注册到 LLM。
"""

from __future__ import annotations

import asyncio
import time
from typing import Optional

from core.logging_manager import get_logger

from .memory import MemoryManager
from .memory.memory_paths import ENTITY_USER, ENTITY_GROUP

logger = get_logger("kiraos_memory_tools", "green")

# 工具描述文案（沿用 lightning 调优过的中文 prompt）
TOOL_SCHEMAS: dict = {
    "memory_add": {
        "description": (
            "把一条事实/反思写入长期记忆。常见触发：用户透露身份/地点/职业/关系/偏好/经历。"
            "系统自动按 entity 隔离存储；省略 entity_id 时默认当前发言者。"
            "内部已做两级去重（SHA-256 哈希 + FTS5 语义），重复内容会自动合并。"
        ),
        "params": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "要记录的记忆文本"},
                "entity_id": {
                    "type": "string",
                    "description": "目标实体 ID（用户号 / 群号）。省略则默认当前发言者。",
                },
                "entity_type": {
                    "type": "string",
                    "description": "实体类型，默认 user",
                    "enum": ["user", "group", "channel"],
                },
                "importance": {
                    "type": "number",
                    "description": "重要性 1-10（默认 5）",
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "标签列表（可选）",
                },
                "memory_type": {
                    "type": "string",
                    "description": "记忆类型，默认 fact",
                    "enum": ["fact", "reflection"],
                },
            },
            "required": ["text"],
        },
    },
    "memory_search": {
        "description": (
            "在长期记忆中搜索相关条目。省略 entity_id 时默认搜索当前发言者；"
            "也可以用逗号分隔传入多个 entity_id 做并行多用户搜索。"
        ),
        "params": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索查询文本"},
                "entity_id": {
                    "type": "string",
                    "description": "目标实体 ID（可逗号分隔多个）。省略默认当前发言者。",
                },
                "entity_type": {
                    "type": "string",
                    "description": "实体类型",
                    "enum": ["user", "group", "channel"],
                },
                "k": {
                    "type": "number",
                    "description": "返回结果数量，默认 5",
                },
            },
            "required": ["query"],
        },
    },
    "memory_update_entry": {
        "description": "覆盖一条已有记忆的文本/重要性。memory_id 通过先 memory_search 获得。",
        "params": {
            "type": "object",
            "properties": {
                "memory_id": {"type": "string", "description": "记忆 ID"},
                "text": {"type": "string", "description": "更新后的记忆文本"},
                "entity_id": {"type": "string", "description": "目标实体 ID"},
                "entity_type": {
                    "type": "string",
                    "description": "实体类型",
                    "enum": ["user", "group", "channel"],
                },
                "folder": {
                    "type": "string",
                    "description": "所在目录，默认 facts",
                    "enum": ["facts", "reflections"],
                },
                "importance": {
                    "type": "number",
                    "description": "新的重要性 1-10（可选）",
                },
            },
            "required": ["memory_id", "text"],
        },
    },
    "memory_remove": {
        "description": "把一条记忆移入归档。memory_id 通过先 memory_search 获得。",
        "params": {
            "type": "object",
            "properties": {
                "memory_id": {"type": "string", "description": "记忆 ID"},
                "entity_id": {"type": "string", "description": "目标实体 ID"},
                "entity_type": {
                    "type": "string",
                    "description": "实体类型",
                    "enum": ["user", "group", "channel"],
                },
                "folder": {
                    "type": "string",
                    "description": "所在目录，默认 facts",
                    "enum": ["facts", "reflections"],
                },
            },
            "required": ["memory_id"],
        },
    },
    "profile_view": {
        "description": "查看用户画像（姓名/昵称/特征/偏好/关系/事实/互动次数）。省略 entity_id 默认当前发言者。",
        "params": {
            "type": "object",
            "properties": {
                "entity_id": {"type": "string", "description": "目标实体 ID"},
                "entity_type": {
                    "type": "string",
                    "description": "实体类型",
                    "enum": ["user", "group", "channel"],
                },
            },
            "required": [],
        },
    },
    "profile_update": {
        "description": "更新用户画像：增加特征 / 删除特征 / 增加事实 / 设置姓名 / 设置关系。",
        "params": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "add_trait",
                        "remove_trait",
                        "add_fact",
                        "set_name",
                        "set_relationship",
                    ],
                    "description": "操作类型",
                },
                "value": {"type": "string", "description": "操作值"},
                "entity_id": {"type": "string", "description": "目标实体 ID"},
                "entity_type": {
                    "type": "string",
                    "description": "实体类型",
                    "enum": ["user", "group", "channel"],
                },
                "target": {
                    "type": "string",
                    "description": "关系目标（仅 action=set_relationship 时必填）",
                },
            },
            "required": ["action", "value"],
        },
    },
}


_TYPE_LABELS = {
    "fact": "事实",
    "reflection": "洞察",
    "episodic": "事件",
    "summary": "摘要",
}


def _primary_user_id(event) -> str:
    """从 event 取出主发言者的 user_id"""
    try:
        if event and event.messages:
            last = event.messages[-1]
            if last.sender and last.sender.user_id:
                return str(last.sender.user_id)
    except AttributeError:
        pass
    return ""


def _resolve_entity(event, entity_id: str, entity_type: str) -> tuple[str, str]:
    """缺省 entity_id 时回退到当前发言者；entity_type 为空时默认 user"""
    if not entity_type:
        entity_type = ENTITY_USER
    if not entity_id:
        entity_id = _primary_user_id(event)
    return entity_id, entity_type


def _format_memories(memories, entity_id: str = "") -> str:
    if not memories:
        return ""
    lines = []
    for mem in memories:
        label = _TYPE_LABELS.get(mem.type, mem.type)
        tags = f" [{', '.join(mem.tags)}]" if mem.tags else ""
        prefix = f"[{entity_id}] " if entity_id else ""
        lines.append(f"{prefix}[{label}]{tags} id={mem.id} {mem.raw_text}")
    return "\n".join(lines)


# ────────────────────────────────────────────────────────────────────
#  Tool implementations
# ────────────────────────────────────────────────────────────────────

async def memory_add(
    manager: MemoryManager,
    event,
    *,
    text: str,
    entity_id: str = "",
    entity_type: str = "user",
    importance: int = 5,
    tags: list = None,
    memory_type: str = "fact",
) -> str:
    if not manager or not getattr(manager, "tree_store", None):
        return "Memory system not available"
    if not text or not text.strip():
        return "Error: text is required"

    entity_id, entity_type = _resolve_entity(event, entity_id, entity_type)
    if not entity_id:
        return "Error: cannot determine entity_id"

    try:
        importance = max(1, min(10, int(importance)))
    except (TypeError, ValueError):
        importance = 5

    folder = "reflections" if memory_type == "reflection" else "facts"

    try:
        # 走去重管线（与海马体一致）
        extractor = getattr(manager, "extractor", None)
        if extractor and extractor._llm_client is not None:
            decision, matched = await extractor.deduplicate(
                text, entity_id, entity_type, folder
            )
            if decision == "duplicate":
                return f"Memory already exists (duplicate detected), skipped"
            if decision == "update" and matched:
                merged_text = await extractor.merge_facts(matched.text, text)
                matched.text = merged_text
                matched.importance = max(matched.importance, importance)
                await manager.tree_store.update_memory(matched)
                return f"Memory merged into existing: id={matched.id}, entity={entity_id}"

        entry = await manager.tree_store.add_memory(
            content_text=text,
            memory_type=memory_type,
            importance=importance,
            tags=list(tags) if tags else [],
            entity_id=entity_id,
            entity_type=entity_type,
            folder=folder,
        )
        return f"Memory added: id={entry.id}, type={memory_type}, entity={entity_id}"
    except Exception as e:
        logger.error(f"memory_add error: {e}")
        return f"Failed to add memory: {e}"


async def memory_search(
    manager: MemoryManager,
    event,
    *,
    query: str,
    entity_id: str = "",
    entity_type: str = "user",
    k: int = 5,
) -> str:
    if not manager or not hasattr(manager, "recall"):
        return "Memory system not available"
    if not query or not query.strip():
        return "Error: query is required"

    try:
        k = max(1, int(k))
    except (TypeError, ValueError):
        k = 5

    # 拆分多 entity（逗号分隔）
    if entity_id and "," in entity_id:
        names = [n.strip() for n in entity_id.split(",") if n.strip()]
        if len(names) > 1:
            tasks = [
                manager.recall(query, entity_id=n, entity_type=entity_type or ENTITY_USER, k=k)
                for n in names
            ]
            all_results = await asyncio.gather(*tasks, return_exceptions=True)
            parts = []
            for name, result in zip(names, all_results):
                if isinstance(result, Exception):
                    logger.warning(f"memory_search failed for {name}: {result}")
                    continue
                formatted = _format_memories(result, entity_id=name)
                if formatted:
                    parts.append(formatted)
            return "\n".join(parts) if parts else "No relevant memories found"

    entity_id, entity_type = _resolve_entity(event, entity_id, entity_type)
    if not entity_id:
        return "Error: cannot determine entity_id"

    memories = await manager.recall(
        query, entity_id=entity_id, entity_type=entity_type, k=k
    )
    return _format_memories(memories) or "No relevant memories found"


async def memory_update_entry(
    manager: MemoryManager,
    event,
    *,
    memory_id: str,
    text: str,
    entity_id: str = "",
    entity_type: str = "user",
    folder: str = "facts",
    importance: Optional[int] = None,
) -> str:
    if not manager or not getattr(manager, "tree_store", None):
        return "Memory system not available"

    entity_id, entity_type = _resolve_entity(event, entity_id, entity_type)
    if not entity_id:
        return "Error: cannot determine entity_id"

    memory = await manager.tree_store.get_memory(
        memory_id=memory_id,
        entity_id=entity_id,
        entity_type=entity_type,
        folder=folder,
    )
    if not memory:
        return f"Memory not found: {memory_id}"

    memory.text = text
    memory.meta["last_accessed"] = time.time()
    if importance is not None:
        try:
            memory.importance = max(1, min(10, int(importance)))
        except (TypeError, ValueError):
            pass

    if await manager.tree_store.update_memory(memory):
        return f"Memory updated: {memory_id}"
    return f"Failed to update memory: {memory_id}"


async def memory_remove(
    manager: MemoryManager,
    event,
    *,
    memory_id: str,
    entity_id: str = "",
    entity_type: str = "user",
    folder: str = "facts",
) -> str:
    if not manager or not getattr(manager, "tree_store", None):
        return "Memory system not available"

    entity_id, entity_type = _resolve_entity(event, entity_id, entity_type)
    if not entity_id:
        return "Error: cannot determine entity_id"

    if await manager.tree_store.archive_memory(
        memory_id=memory_id,
        entity_id=entity_id,
        entity_type=entity_type,
        folder=folder,
    ):
        return f"Memory archived: {memory_id}"
    return f"Failed to archive memory: {memory_id}"


async def profile_view(
    manager: MemoryManager,
    event,
    *,
    entity_id: str = "",
    entity_type: str = "user",
) -> str:
    if not manager or not getattr(manager, "profile_store", None):
        return "Profile system not available"

    entity_id, entity_type = _resolve_entity(event, entity_id, entity_type)
    if not entity_id:
        return "Error: cannot determine entity_id"

    return await manager.profile_store.get_profile_prompt(entity_id, entity_type)


async def profile_update(
    manager: MemoryManager,
    event,
    *,
    action: str,
    value: str,
    entity_id: str = "",
    entity_type: str = "user",
    target: str = "",
) -> str:
    if not manager or not getattr(manager, "profile_store", None):
        return "Profile system not available"
    if not action or not value:
        return "Error: action and value are required"

    entity_id, entity_type = _resolve_entity(event, entity_id, entity_type)
    if not entity_id:
        return "Error: cannot determine entity_id"

    store = manager.profile_store

    if action == "add_trait":
        await store.add_trait(entity_id, value, entity_type)
        return f"Added trait '{value}'"
    if action == "remove_trait":
        await store.remove_trait(entity_id, value, entity_type)
        return f"Removed trait '{value}'"
    if action == "add_fact":
        await store.add_fact(entity_id, value, entity_type)
        return "Added fact"
    if action == "set_name":
        await store.update_profile(entity_id, entity_type, name=value)
        return f"Set name '{value}'"
    if action == "set_relationship":
        if not target:
            return "Error: target is required for set_relationship"
        await store.set_relationship(entity_id, target, value, entity_type)
        return f"Set relationship '{value}' with '{target}'"

    return f"Unknown action: {action}"
