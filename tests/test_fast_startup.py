"""Fast startup: index freshness check and serialized plugin lifecycle.

KiraOS used to rebuild 20k+ TOML files on every plugin load. KiraAI also
re-initializes a plugin after every WebUI config save, so the rebuild ran while
the save request was waiting — the UI looked frozen, and a concurrent
terminate could null out memory_manager mid-initialize. These tests pin the
count-based freshness check and the initialize/terminate lock.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import tomli_w

from plugins.KiraOS_Plugin.main import UserMemoryPlugin
from plugins.KiraOS_Plugin.memory.memory_index import MemoryIndex
from plugins.KiraOS_Plugin.memory.memory_manager import MemoryManager
from plugins.KiraOS_Plugin.memory.memory_paths import (
    get_entity_dir,
    set_data_root,
)
from plugins.KiraOS_Plugin.memory.toml_tree_store import TomlTreeStore


def _env(data_root):
    set_data_root(data_root / "memory")
    index = MemoryIndex()
    store = TomlTreeStore(index=index)
    return data_root, store


async def _add(store, text, semantic_id, entity_id="onebot:1"):
    return await store.add_memory(
        content_text=text,
        entity_id=entity_id,
        entity_type="user",
        folder="facts",
        semantic_id=semantic_id,
    )


def _write_extra_toml(data_root, entity_id="onebot:999", mem_id="extra"):
    folder = Path(get_entity_dir(entity_id, "user")) / "facts"
    folder.mkdir(parents=True, exist_ok=True)
    payload = {
        "id": mem_id,
        "type": "fact",
        "text": "手动添加的记忆",
        "importance": 5,
        "tags": [],
    }
    (folder / f"{mem_id}.toml").write_bytes(tomli_w.dumps(payload).encode("utf-8"))


def test_fresh_index_skips_rebuild(data_root):
    root, store = _env(data_root)
    try:
        asyncio.run(_add(store, "用户喜欢猫", "likes_cats"))
        asyncio.run(_add(store, "用户住在南京", "lives_nanjing"))
        result = asyncio.run(store.ensure_index_fresh())
        assert result["rebuilt"] is False
        assert result["index"] == 2
        assert result["files"] == 2
    finally:
        store.close()


def test_extra_toml_file_triggers_rebuild(data_root):
    root, store = _env(data_root)
    try:
        asyncio.run(_add(store, "用户喜欢猫", "likes_cats"))
        _write_extra_toml(root)
        result = asyncio.run(store.ensure_index_fresh())
        assert result["rebuilt"] is True
        assert result["reason"] == "count_mismatch"
        assert store.index.count_all() == 2
        hits = asyncio.run(store.search("手动", entity_id="onebot:999", folder="facts"))
        assert any(hit.text == "手动添加的记忆" for hit in hits)
    finally:
        store.close()


def test_deleted_toml_file_triggers_rebuild(data_root):
    root, store = _env(data_root)
    try:
        first = asyncio.run(_add(store, "用户喜欢猫", "likes_cats"))
        asyncio.run(_add(store, "用户住在南京", "lives_nanjing"))
        Path(first.file_path).unlink()
        result = asyncio.run(store.ensure_index_fresh())
        assert result["rebuilt"] is True
        assert store.index.count_all() == 1
    finally:
        store.close()


def test_force_rebuild(data_root):
    root, store = _env(data_root)
    try:
        asyncio.run(_add(store, "用户喜欢猫", "likes_cats"))
        result = asyncio.run(store.ensure_index_fresh(force=True))
        assert result["rebuilt"] is True
        assert result["reason"] == "forced"
    finally:
        store.close()


def test_empty_store_is_fresh_without_rebuild(data_root):
    root, store = _env(data_root)
    try:
        result = asyncio.run(store.ensure_index_fresh())
        assert result["rebuilt"] is False
        assert result["index"] == 0
        assert result["files"] == 0
    finally:
        store.close()


def test_manager_async_init_skips_rebuild_when_fresh(data_root):
    set_data_root(data_root / "memory")
    manager = MemoryManager(
        max_memory_length=20,
        hippocampus_threshold=99,
        llm_chat_timeout=30,
        enable_episode_memory=False,
        persist_hippocampus_pending=False,
    )
    try:
        asyncio.run(_add(manager.tree_store, "用户喜欢猫", "likes_cats"))

        async def _boom():
            raise AssertionError("fresh index must not be rebuilt")

        manager.tree_store.rebuild_index = _boom  # type: ignore[method-assign]
        asyncio.run(manager.async_init())
    finally:
        manager.close()


def test_initialize_and_terminate_are_serialized():
    events = []

    class Probe(UserMemoryPlugin):
        async def _initialize_unlocked(self):
            events.append("init_start")
            await asyncio.sleep(0.05)
            events.append("init_end")

        async def _terminate_unlocked(self):
            events.append("term_start")
            await asyncio.sleep(0.02)
            events.append("term_end")

    plugin = Probe(ctx=None, cfg={})

    async def run():
        await asyncio.gather(plugin.initialize(), plugin.terminate())

    asyncio.run(run())
    assert events == ["init_start", "init_end", "term_start", "term_end"]


def test_two_initializes_do_not_overlap():
    events = []

    class Probe(UserMemoryPlugin):
        async def _initialize_unlocked(self):
            events.append("start")
            await asyncio.sleep(0.02)
            events.append("end")

    plugin = Probe(ctx=None, cfg={})

    async def run():
        await asyncio.gather(plugin.initialize(), plugin.initialize())

    asyncio.run(run())
    assert events == ["start", "end", "start", "end"]
