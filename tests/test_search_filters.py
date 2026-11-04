"""Rich memory_search filters: folder / since / until / tags.

The QanMoCore retrieval agent could search chat history by participant and time
range. KiraOS's fast recall was query-only. These filters let the main LLM do
multi-step "deep recall" itself: search facts, reflections and episodes with
different keywords/time windows instead of a nested agent.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from types import SimpleNamespace

import pytest

from core.chat.message_elements import Text
from plugins.KiraOS_Plugin import tools
from plugins.KiraOS_Plugin.memory.memory_manager import MemoryManager
from plugins.KiraOS_Plugin.memory.memory_paths import set_data_root


def _ts(year, month, day):
    return datetime(year, month, day).astimezone().timestamp()


def _event(text="显卡"):
    message = SimpleNamespace(
        sender=SimpleNamespace(user_id="123", nickname="小明"),
        message=SimpleNamespace(chain=[Text(text)]),
    )
    return SimpleNamespace(
        adapter=SimpleNamespace(name="onebot"),
        session=SimpleNamespace(session_type="dm", session_id="123"),
        messages=[message],
    )


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def manager(data_root):
    set_data_root(data_root / "memory")
    mgr = MemoryManager(
        max_memory_length=20,
        hippocampus_threshold=99,
        llm_chat_timeout=30,
        enable_episode_memory=True,
        persist_hippocampus_pending=False,
    )
    _run(_seed(mgr))
    yield mgr
    mgr.close()


async def _seed(manager):
    async def add(text, folder, tags, timestamp, semantic_id, memory_type="fact"):
        memory = await manager.tree_store.add_memory(
            content_text=text,
            memory_type=memory_type,
            importance=6,
            tags=tags,
            entity_id="onebot:123",
            entity_type="user",
            folder=folder,
            semantic_id=semantic_id,
        )
        memory.meta["timestamp"] = timestamp
        await manager.tree_store.update_memory(memory)

    await add("A6000 显卡价格讨论", "facts", ["gpu", "硬件"], _ts(2026, 5, 1), "gpu_fact")
    await add("猫娘表情包收藏", "facts", ["cat"], _ts(2026, 6, 1), "cat_fact")
    await add(
        "群友讨论了 A6000 显卡事件",
        "episodes",
        ["episode", "gpu"],
        _ts(2026, 5, 2),
        "gpu_episode",
        memory_type="episode",
    )


def test_store_filters_by_tags(manager):
    results = _run(
        manager.tree_store.search(
            "显卡", entity_id="onebot:123", entity_type="user", folder="facts", tags=["gpu"]
        )
    )
    assert [m.id for m in results] == ["gpu_fact"]
    results = _run(
        manager.tree_store.search(
            "猫娘", entity_id="onebot:123", entity_type="user", folder="facts", tags=["gpu"]
        )
    )
    assert results == []


def test_store_filters_by_time_range(manager):
    since = _ts(2026, 5, 15)
    results = _run(
        manager.tree_store.search(
            "猫娘", entity_id="onebot:123", entity_type="user", folder="facts", since=since
        )
    )
    assert [m.id for m in results] == ["cat_fact"]

    until = _ts(2026, 5, 15)
    results = _run(
        manager.tree_store.search(
            "显卡", entity_id="onebot:123", entity_type="user", folder="facts", until=until
        )
    )
    assert [m.id for m in results] == ["gpu_fact"]


def test_recall_includes_episodes_by_default(manager):
    results = _run(manager.recall("显卡", entity_id="onebot:123", entity_type="user", k=5))
    assert any(m.type == "episode" for m in results)
    assert any(m.type == "fact" for m in results)


def test_memory_search_tool_filters_folder_and_tags(manager):
    result = _run(
        tools.memory_search(
            manager,
            _event(),
            query="显卡",
            folder="episodes",
            tags=["episode"],
            k=5,
        )
    )
    assert "gpu_episode" in result
    assert "gpu_fact" not in result


def test_memory_search_tool_time_filters(manager):
    result = _run(
        tools.memory_search(
            manager,
            _event(),
            query="猫娘",
            folder="facts",
            since="2026-05-15",
            k=5,
        )
    )
    assert "cat_fact" in result

    empty = _run(
        tools.memory_search(
            manager,
            _event(),
            query="猫娘",
            folder="facts",
            until="2026-05-15",
            k=5,
        )
    )
    assert "No relevant memories found" in empty


def test_memory_search_rejects_bad_filters(manager):
    bad_folder = _run(
        tools.memory_search(manager, _event(), query="x", folder="unknown")
    )
    assert "folder" in bad_folder
    bad_time = _run(
        tools.memory_search(manager, _event(), query="x", since="not-a-date")
    )
    assert "invalid time" in bad_time
    bad_order = _run(
        tools.memory_search(
            manager,
            _event(),
            query="x",
            since="2026-06-01",
            until="2026-05-01",
        )
    )
    assert "since" in bad_order
