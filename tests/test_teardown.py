"""Deterministic teardown of the SQLite handle.

`MemoryIndex.close()` existed but nothing ever called it — `terminate()` just
dropped the manager reference and left the connection to garbage collection.
Two consequences: a disable→re-enable cycle in the WebUI reruns
`initialize()` and can hold two connections at once, and on Windows an open db
file blocks removal of the data directory (PermissionError on uninstall).

The chain is `plugin.terminate()` -> `MemoryManager.close()` ->
`TomlTreeStore.close()` -> `MemoryIndex.close()`, ordered after the
background-task drain because in-flight hippocampus tasks still write the
index.
"""

from __future__ import annotations

import asyncio

import pytest

from plugins.KiraOS_Plugin.memory.memory_paths import (
    set_data_root,
    ensure_directory_structure,
    get_index_db_path,
)
from plugins.KiraOS_Plugin.memory.memory_index import MemoryIndex
from plugins.KiraOS_Plugin.memory.toml_tree_store import TomlTreeStore
from plugins.KiraOS_Plugin.memory.memory_manager import MemoryManager


@pytest.fixture
def rooted(tmp_path):
    set_data_root(tmp_path / "memory")
    ensure_directory_structure()
    return tmp_path / "memory"


def test_index_close_releases_the_connection(rooted):
    index = MemoryIndex(db_path=get_index_db_path())
    assert index._conn is not None, "constructor should have opened the db"
    index.close()
    assert index._conn is None


def test_index_reconnects_lazily_after_close(rooted):
    """close() must leave the object usable, not bricked.

    The WebUI and in-flight background tasks can hold the same MemoryIndex
    across a plugin disable→re-enable cycle, so requests do arrive after
    close(). Without a reconnect they hit
    `AttributeError: 'NoneType' object has no attribute 'cursor'` — a plain
    "connection is closed" state surfacing as a crash.
    """
    index = MemoryIndex(db_path=get_index_db_path())
    index.close()
    assert index._conn is None

    # Any read or write should transparently reopen.
    assert index.count_memories() == 0
    assert index._conn is not None
    index.close()


def test_store_close_delegates_to_the_index(rooted):
    index = MemoryIndex(db_path=get_index_db_path())
    store = TomlTreeStore(index=index)

    async def touch():
        await store.add_memory(
            content_text="打开连接", entity_id="onebot:1",
            entity_type="user", folder="facts",
        )

    asyncio.new_event_loop().run_until_complete(touch())
    assert index._conn is not None

    store.close()
    assert index._conn is None, "TomlTreeStore.close did not reach the index"


def test_manager_close_delegates_through_the_chain(rooted):
    mgr = MemoryManager(
        max_memory_length=20, hippocampus_threshold=3, llm_chat_timeout=30
    )

    async def touch():
        await mgr.tree_store.add_memory(
            content_text="打开连接", entity_id="onebot:1",
            entity_type="user", folder="facts",
        )

    asyncio.new_event_loop().run_until_complete(touch())
    assert mgr.memory_index._conn is not None

    mgr.close()
    assert mgr.memory_index._conn is None, (
        "MemoryManager.close did not reach MemoryIndex"
    )


def test_close_is_idempotent(rooted):
    mgr = MemoryManager(
        max_memory_length=20, hippocampus_threshold=3, llm_chat_timeout=30
    )
    mgr.close()
    mgr.close()  # must not raise


def test_store_remains_usable_after_reopen(rooted):
    """close() must not poison the object — the index lazily reconnects, which
    is what makes a disable→re-enable cycle survivable."""
    index = MemoryIndex(db_path=get_index_db_path())
    store = TomlTreeStore(index=index)
    loop = asyncio.new_event_loop()

    async def write(text, mid):
        return await store.add_memory(
            content_text=text, semantic_id=mid, entity_id="onebot:1",
            entity_type="user", folder="facts",
        )

    loop.run_until_complete(write("第一条", "first"))
    store.close()

    loop.run_until_complete(write("第二条", "second"))
    got = loop.run_until_complete(
        store.get_memory("second", entity_id="onebot:1",
                         entity_type="user", folder="facts")
    )
    assert got is not None and got.text == "第二条"
    store.close()
