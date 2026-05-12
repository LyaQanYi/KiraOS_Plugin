"""LLM tool implementations (lightning-style) backed by MemoryManager.

These are pure logic functions taking ``(event, manager, **kwargs)`` and
returning a string. The plugin class in ``main.py`` registers them via the
KiraAI ``@register_tool`` decorator and forwards calls here, so the tool
surface remains independently testable.

Tool roster:
  - memory_add        — write a new memory through the dedup pipeline
  - memory_update_tool— edit an existing memory by id
  - memory_remove     — archive (soft delete) a memory by id
  - memory_search     — semantic recall (single or multi-entity)
  - profile_view      — show an entity's profile as text
  - profile_update    — mutate an entity profile (trait / fact / relationship)

The smart-entity-resolver (group-id rejection → nickname reverse-lookup →
fast-LLM extraction from chat context → speaker fallback) is adapted from
KiraAI-lightning's ``data/tools/memory.py``.
"""

from __future__ import annotations

import asyncio
from typing import Tuple

from core.logging_manager import get_logger

from ..memory.memory_manager import MemoryManager
from ..memory.memory_paths import list_all_entities
from ..memory.toml_tree_store import Memory

# ``LLMRequest`` / ``Prompt`` are imported lazily inside the helper that uses
# them — see comment in memory_extractor.py for the circular-import rationale.

logger = get_logger("kiraos_memory_tools", "green")

# Globals injected by the plugin during ``initialize()``.
_memory_manager: MemoryManager | None = None
_fast_llm_client = None


def set_memory_manager(manager: MemoryManager | None):
    global _memory_manager
    _memory_manager = manager


def set_fast_llm_client(client):
    global _fast_llm_client
    _fast_llm_client = client


# ==========================================
# Entity resolution helpers
# ==========================================


def _adapter_of(event) -> str:
    """Read ``event.adapter.name``; KiraAI guarantees ``event.adapter`` exists
    on a real ``KiraMessageBatchEvent`` but tests may pass a stub."""
    try:
        return event.adapter.name
    except AttributeError:
        return "unknown"


def _primary_sender_id(event) -> str:
    """Most recent message's sender_id."""
    try:
        for msg in reversed(event.messages):
            sid = msg.sender.user_id if msg.sender else ""
            if sid and sid != "unknown":
                return sid
    except (AttributeError, IndexError):
        pass
    return ""


def _resolve_entity_from_event(event) -> Tuple[str, str]:
    """Default fallback: always attribute to the current speaker."""
    sender_id = _primary_sender_id(event)
    adapter = _adapter_of(event)
    if sender_id:
        return f"{adapter}:{sender_id}", "user"
    return "", "user"


def _looks_like_entity_id(entity_id: str) -> bool:
    """``adapter:numeric_id``-style ids are recognised; bare names are not."""
    if not entity_id:
        return False
    if ":" in entity_id:
        parts = entity_id.split(":", 1)
        return len(parts) == 2 and len(parts[1]) > 0
    return False


def _looks_like_group_id(entity_id: str) -> bool:
    """Catch the common LLM mistake of passing a group identifier as entity_id.

    We can't reliably distinguish a personal QQ number from a group number
    purely from digits, so we only flag obviously-group strings (``group:...``
    or "群" keyword).
    """
    if not entity_id:
        return False
    eid = entity_id.strip()
    lower = eid.lower()
    if "group" in lower or "群" in eid:
        return True
    return False


async def _resolve_entity_id_by_name(
    entity_id: str, entity_type: str
) -> Tuple[str, str]:
    """If ``entity_id`` looks like a nickname rather than ``adapter:id``,
    try to reverse-lookup via the profile store."""
    if not entity_id or _looks_like_entity_id(entity_id):
        return entity_id, entity_type
    if _memory_manager is None:
        return entity_id, entity_type
    try:
        resolved = await _memory_manager.profile_store.resolve_entity_by_name(
            entity_id, entity_type
        )
        if resolved:
            logger.info(
                f"Nickname resolved: '{entity_id}' → {resolved} ({entity_type})"
            )
            return resolved, entity_type
    except Exception as e:
        logger.warning(f"Profile reverse-lookup failed: {e}")
    logger.debug(f"Could not resolve nickname '{entity_id}', using as-is")
    return entity_id, entity_type


async def _get_known_users_hint() -> str:
    """Build a compact "known users" hint for the fast-LLM entity extractor."""
    if _memory_manager is None:
        return ""
    try:
        users = []
        for eid, etype in list_all_entities("user"):
            try:
                profile = await _memory_manager.profile_store.get_profile(eid, etype)
            except Exception:
                continue
            names = []
            if profile.name:
                names.append(profile.name)
            if profile.nickname and profile.nickname != profile.name:
                names.append(profile.nickname)
            for a in profile.aliases:
                if a and a not in names:
                    names.append(a)
            if names:
                users.append(f"  {eid} → {'/'.join(names)}")
        if users:
            return "\n已知用户：\n" + "\n".join(users)
    except Exception:
        pass
    return ""


async def _extract_entities_from_context(query: str, event=None) -> list[str]:
    """Use the fast LLM to extract referenced person identifiers from the
    query (and recent conversation context). Returns nicknames / QQ numbers;
    ``SELF`` / ``NONE`` are filtered out by the caller."""
    if _fast_llm_client is None or not query:
        return []

    known_hint = await _get_known_users_hint()
    context = _format_recent_messages(event)

    prompt = (
        "从以下查询和对话上下文中, 提取所有被提及的人物标识(昵称或QQ号).\n"
        "规则:\n"
        '- 如果查询是关于当前发言者自己的(如"我喜欢...", "记住我..."), 返回 SELF\n'
        "- 如果涉及其他用户, 返回他们的昵称或QQ号, 每行一个\n"
        "- 如果无法确定具体人物, 返回 NONE\n"
        "- 不要输出任何解释, 只输出标识\n"
        f"{known_hint}\n\n"
        f"查询: {query}\n"
        f"对话上下文: {context or 'N/A'}\n\n"
        "提取的人物标识(每行一个):"
    )

    try:
        from core.provider.llm_model import LLMRequest
        from core.prompt_manager import Prompt
        req = LLMRequest(
            user_prompt=[Prompt(prompt, name="entity_extract", source="kiraos")],
        )
        req.assemble_prompt()
        resp = await _fast_llm_client.chat(req)
        if not resp or not resp.text_response:
            return []
        raw = resp.text_response.strip()
        lines = [l.strip() for l in raw.split("\n") if l.strip()]
        entities = [l for l in lines if l not in ("SELF", "NONE", "UNKNOWN", "无", "")]
        logger.info(f"Entity extraction: query='{query[:30]}' → {entities}")
        return entities
    except Exception as e:
        logger.warning(f"Entity extraction via fast_llm failed: {e}")
        return []


def _format_recent_messages(event, max_chars: int = 800) -> str:
    """Concatenate the most recent KiraMessageBatchEvent text content."""
    if event is None or not getattr(event, "messages", None):
        return ""
    try:
        from core.chat.message_elements import Text
    except ImportError:
        return ""
    parts = []
    for msg in event.messages[-10:]:
        sender_name = msg.sender.nickname if msg.sender else ""
        chain = getattr(getattr(msg, "message", None), "chain", None)
        if not chain:
            continue
        text = "".join(
            elem.text for elem in chain if isinstance(elem, Text)
        ).strip()
        if not text:
            continue
        prefix = f"{sender_name}: " if sender_name else ""
        parts.append(f"{prefix}{text}")
    joined = "\n".join(parts)
    return joined[-max_chars:] if len(joined) > max_chars else joined


async def _smart_resolve_entity(
    entity_id: str, entity_type: str, query: str, event=None
) -> Tuple[str, str]:
    """Five-step entity resolver:

    1. Group-id passed in → drop it, fall through (LLMs commonly mis-route to
       a group number).
    2. Caller passed a real ``entity_id`` → optionally reverse-lookup if it's
       actually a nickname.
    3. No id + fast LLM available → extract from conversation context.
    4. Still nothing → current speaker.
    5. No event → empty entity_id (caller errors out).
    """
    if entity_id and _looks_like_group_id(entity_id):
        logger.warning(
            f"Rejected group-like entity_id: '{entity_id}', falling back to auto-resolve"
        )
        entity_id = ""

    if entity_id:
        return await _resolve_entity_id_by_name(entity_id, entity_type)

    if _fast_llm_client is not None:
        extracted = await _extract_entities_from_context(query, event)
        for name in extracted:
            resolved_id, resolved_type = await _resolve_entity_id_by_name(name, "user")
            if resolved_id and _looks_like_entity_id(resolved_id):
                return resolved_id, resolved_type

    if event is not None:
        return _resolve_entity_from_event(event)

    return "", "user"


# ==========================================
# Memory tools
# ==========================================


_TYPE_LABELS = {
    "fact": "事实",
    "reflection": "洞察",
    "episodic": "事件",
    "summary": "摘要",
    "skill": "技能",
}


def _format_memories(memories: list, entity_id: str = "") -> str:
    if not memories:
        return ""
    lines = []
    for mem in memories:
        label = _TYPE_LABELS.get(mem.type, mem.type)
        tags = f" [{', '.join(mem.tags)}]" if mem.tags else ""
        prefix = f"[{entity_id}] " if entity_id else ""
        lines.append(f"{prefix}[{label}]{tags} id={mem.id} {mem.raw_text}")
    return "\n".join(lines)


async def memory_add(
    event,
    *,
    text: str,
    entity_id: str = "",
    entity_type: str = "user",
    importance: int = 5,
    tags: list | None = None,
    memory_type: str = "fact",
) -> str:
    """Add a memory through the dedup pipeline (SHA-256 → FTS5 → LLM judge)."""
    if _memory_manager is None:
        return "Error: memory system not initialized"
    if not text or not text.strip():
        return "Error: text is required"

    entity_id, entity_type = await _smart_resolve_entity(
        entity_id, entity_type, text, event
    )
    if not entity_id:
        return "Error: cannot determine target entity_id"

    try:
        importance = max(1, min(10, int(importance)))
    except (TypeError, ValueError):
        importance = 5

    folder_map = {"fact": "facts", "reflection": "reflections"}
    folder = folder_map.get(memory_type, "facts")

    try:
        if _memory_manager.extractor:
            decision, matched = await _memory_manager.extractor.deduplicate(
                text, entity_id, entity_type, folder
            )
            if decision == "duplicate":
                return f"Memory already exists (duplicate skipped) for {entity_id}"
            if decision == "update" and matched:
                merged_text = await _memory_manager.extractor.merge_facts(
                    matched.text, text
                )
                matched.text = merged_text
                matched.importance = max(matched.importance, importance)
                if tags:
                    existing_tags = set(matched.tags)
                    existing_tags.update(tags)
                    matched.tags = list(existing_tags)
                await _memory_manager.tree_store.update_memory(matched)
                return f"Memory merged into existing id={matched.id} for {entity_id}"

        entry = await _memory_manager.tree_store.add_memory(
            content_text=text,
            memory_type=memory_type,
            importance=importance,
            tags=tags or [],
            entity_id=entity_id,
            entity_type=entity_type,
            folder=folder,
        )
        return f"Memory added: id={entry.id} type={memory_type} entity={entity_id}"
    except Exception as e:
        logger.error(f"memory_add error: {e}")
        return f"Failed to add memory: {e}"


async def memory_update_tool(
    event,
    *,
    memory_id: str,
    text: str,
    entity_id: str = "",
    entity_type: str = "user",
    folder: str = "facts",
    importance: int | None = None,
) -> str:
    """Update an existing memory's text / importance in place."""
    if _memory_manager is None:
        return "Error: memory system not initialized"
    if not memory_id or not text:
        return "Error: memory_id and text are required"

    entity_id, entity_type = await _smart_resolve_entity(
        entity_id, entity_type, text, event
    )
    if not entity_id:
        return "Error: cannot determine target entity_id"

    memory = await _memory_manager.tree_store.get_memory(
        memory_id=memory_id,
        entity_id=entity_id,
        entity_type=entity_type,
        folder=folder,
    )
    if not memory:
        return f"Memory not found: {memory_id}"

    memory.text = text
    if importance is not None:
        try:
            memory.importance = max(1, min(10, int(importance)))
        except (TypeError, ValueError):
            pass

    if await _memory_manager.tree_store.update_memory(memory):
        return f"Memory updated: {memory_id}"
    return f"Failed to update memory: {memory_id}"


async def memory_remove(
    event,
    *,
    memory_id: str,
    entity_id: str = "",
    entity_type: str = "user",
    folder: str = "facts",
) -> str:
    """Archive (soft-delete) a memory by id."""
    if _memory_manager is None:
        return "Error: memory system not initialized"
    if not memory_id:
        return "Error: memory_id is required"

    entity_id, entity_type = await _smart_resolve_entity(
        entity_id, entity_type, memory_id, event
    )
    if not entity_id:
        return "Error: cannot determine target entity_id"

    if await _memory_manager.tree_store.archive_memory(
        memory_id=memory_id,
        entity_id=entity_id,
        entity_type=entity_type,
        folder=folder,
    ):
        return f"Memory archived: {memory_id}"
    return f"Failed to archive memory: {memory_id}"


async def memory_search(
    event,
    *,
    query: str,
    entity_id: str = "",
    entity_type: str = "user",
    k: int = 5,
) -> str:
    """Semantic search across one or more entities.

    ``entity_id`` may be a single id, a single nickname, a comma-separated
    list (multi-user parallel search), or empty (auto-resolve via fast LLM
    or fall back to the current speaker).
    """
    if _memory_manager is None:
        return "Error: memory system not initialized"
    if not query:
        return "Error: query is required"

    try:
        k = max(1, int(k))
    except (TypeError, ValueError):
        k = 5

    if entity_id and _looks_like_group_id(entity_id):
        logger.warning(f"memory_search rejected group-like entity_id '{entity_id}'")
        entity_id = ""

    # Path 1: caller passed entity_id(s)
    if entity_id:
        names = [n.strip() for n in entity_id.split(",") if n.strip()]
        if len(names) == 1:
            eid, etype = await _resolve_entity_id_by_name(names[0], entity_type)
            memories = await _memory_manager.recall(
                query, entity_id=eid, entity_type=etype, k=k
            )
            return _format_memories(memories) or "No relevant memories found"

        resolved = []
        for name in names:
            rid, rtype = await _resolve_entity_id_by_name(name, "user")
            if rid and _looks_like_entity_id(rid):
                resolved.append((rid, rtype))
        if resolved:
            return await _parallel_search(query, resolved, k)
        # all resolve failed → fall through to auto-resolution

    # Path 2: auto-resolve from context via fast LLM
    extracted = []
    if _fast_llm_client is not None:
        extracted = await _extract_entities_from_context(query, event)

    if not extracted:
        fallback_id, fallback_type = (
            _resolve_entity_from_event(event) if event is not None else ("", "user")
        )
        memories = await _memory_manager.recall(
            query, entity_id=fallback_id, entity_type=fallback_type, k=k
        )
        return _format_memories(memories) or "No relevant memories found"

    resolved = []
    for name in extracted:
        rid, rtype = await _resolve_entity_id_by_name(name, "user")
        if rid and _looks_like_entity_id(rid):
            resolved.append((rid, rtype))

    if not resolved:
        fallback_id, fallback_type = (
            _resolve_entity_from_event(event) if event is not None else ("", "user")
        )
        memories = await _memory_manager.recall(
            query, entity_id=fallback_id, entity_type=fallback_type, k=k
        )
        return _format_memories(memories) or "No relevant memories found"

    return await _parallel_search(query, resolved, k)


async def _parallel_search(query: str, resolved: list, k: int) -> str:
    if len(resolved) == 1:
        eid, etype = resolved[0]
        memories = await _memory_manager.recall(
            query, entity_id=eid, entity_type=etype, k=k
        )
        return _format_memories(memories) or "No relevant memories found"

    tasks = [
        _memory_manager.recall(query, entity_id=eid, entity_type=etype, k=k)
        for eid, etype in resolved
    ]
    all_results = await asyncio.gather(*tasks, return_exceptions=True)

    parts = []
    for i, result in enumerate(all_results):
        if isinstance(result, Exception):
            logger.warning(f"Parallel search failed for {resolved[i][0]}: {result}")
            continue
        formatted = _format_memories(result, entity_id=resolved[i][0])
        if formatted:
            parts.append(formatted)

    return "\n".join(parts) if parts else "No relevant memories found"


async def profile_view(
    event,
    *,
    entity_id: str = "",
    entity_type: str = "user",
) -> str:
    """Return the entity profile as a human-readable prompt block."""
    if _memory_manager is None:
        return "Error: memory system not initialized"

    entity_id, entity_type = await _smart_resolve_entity(
        entity_id, entity_type, "查看用户画像", event
    )
    if not entity_id:
        return "Error: cannot determine target entity_id"

    return await _memory_manager.get_profile_prompt(entity_id, entity_type)


async def profile_update(
    event,
    *,
    action: str,
    value: str,
    entity_id: str = "",
    entity_type: str = "user",
    target: str = "",
) -> str:
    """Mutate the entity profile.

    Actions: ``add_trait``, ``remove_trait``, ``add_fact``, ``set_name``,
    ``set_relationship`` (requires ``target``).
    """
    if _memory_manager is None:
        return "Error: memory system not initialized"
    if not action or not value:
        return "Error: action and value are required"

    entity_id, entity_type = await _smart_resolve_entity(
        entity_id, entity_type, value, event
    )
    if not entity_id:
        return "Error: cannot determine target entity_id"

    store = _memory_manager.profile_store

    try:
        if action == "add_trait":
            await store.add_trait(entity_id, value, entity_type)
            return f"Added trait '{value}' to {entity_id}"
        if action == "remove_trait":
            await store.remove_trait(entity_id, value, entity_type)
            return f"Removed trait '{value}' from {entity_id}"
        if action == "add_fact":
            await store.add_fact(entity_id, value, entity_type)
            return f"Added fact to {entity_id}"
        if action == "set_name":
            await store.update_profile(entity_id, entity_type, name=value)
            return f"Set name '{value}' for {entity_id}"
        if action == "set_relationship":
            if not target:
                return "Error: target is required for set_relationship"
            await store.set_relationship(entity_id, target, value, entity_type)
            return f"Set relationship '{value}' between {entity_id} and {target}"
    except Exception as e:
        logger.error(f"profile_update error: {e}")
        return f"Failed: {e}"

    return f"Unknown action: {action}"
