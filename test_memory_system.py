"""Integration tests for the KiraOS_Plugin memory subsystem.

Ported from KiraAI-lightning's ``test_memory_system.py`` with these changes:
  - imports go through ``KiraOS_Plugin.memory.*`` (treats the plugin as a
    top-level package; see sys.path manipulation below)
  - ``test_memory_router`` is dropped (router is not part of this port)
  - ``test_legacy_migration`` is added to cover the kiraos.db → TOML migrator

Run from the KiraAI repo root:

    PYTHONPATH=. python3 core/plugin/builtin_plugins/KiraOS_Plugin/test_memory_system.py

The test deliberately treats ``KiraOS_Plugin`` as a top-level package (by
inserting ``core/plugin/builtin_plugins`` into ``sys.path``) so we don't
trigger ``core.plugin.__init__`` — which would import KiraAI's full
provider/agent/chat graph and hit a pre-existing circular import that only
``main.py``'s startup ordering resolves.
"""

import asyncio
import os
import shutil
import sqlite3
import sys
import time

# Make ``KiraOS_Plugin`` importable as a top-level package without touching
# ``core.plugin``.
_HERE = os.path.dirname(os.path.abspath(__file__))
_BUILTIN_PLUGINS_DIR = os.path.dirname(_HERE)
if _BUILTIN_PLUGINS_DIR not in sys.path:
    sys.path.insert(0, _BUILTIN_PLUGINS_DIR)
# Also need the repo root so the ``core.logging_manager`` / ``core.utils``
# imports inside the memory submodules work.
_REPO_ROOT = os.path.abspath(os.path.join(_HERE, "..", "..", "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# The memory submodules touch ``core.utils.path_utils.get_data_path()`` at
# module load. Make sure ``<repo>/data`` exists so the log handler can open
# its file before any import below.
os.makedirs(os.path.join(_REPO_ROOT, "data"), exist_ok=True)

try:
    import tomllib
except ImportError:
    import tomli as tomllib  # type: ignore

TEST_DATA_DIR = "data_test_kiraos"
TEST_DB_PATH = os.path.join(TEST_DATA_DIR, "memory", "memory_index.db")

_PKG = "KiraOS_Plugin.memory"


def setup_test_env():
    if os.path.exists(TEST_DATA_DIR):
        shutil.rmtree(TEST_DATA_DIR)

    import importlib
    mp = importlib.import_module(f"{_PKG}.memory_paths")
    mp.MEMORY_ROOT = os.path.join(TEST_DATA_DIR, "memory")
    mp.GLOBAL_DIR = os.path.join(mp.MEMORY_ROOT, "global")
    mp.ENTITIES_DIR = os.path.join(mp.MEMORY_ROOT, "entities")
    mp.ARCHIVE_DIR = os.path.join(mp.MEMORY_ROOT, "archive")

    mi = importlib.import_module(f"{_PKG}.memory_index")
    mi.DEFAULT_DB_PATH = TEST_DB_PATH


def teardown_test_env():
    import gc
    gc.collect()
    try:
        if os.path.exists(TEST_DATA_DIR):
            shutil.rmtree(TEST_DATA_DIR)
    except OSError:
        time.sleep(0.5)
        if os.path.exists(TEST_DATA_DIR):
            shutil.rmtree(TEST_DATA_DIR, ignore_errors=True)


async def test_directory_structure():
    from KiraOS_Plugin.memory.memory_paths import (
        ensure_directory_structure, MEMORY_ROOT, GLOBAL_DIR,
    )
    ensure_directory_structure()
    assert os.path.exists(MEMORY_ROOT)
    assert os.path.exists(os.path.join(GLOBAL_DIR, "facts"))
    assert os.path.exists(os.path.join(GLOBAL_DIR, "skills"))
    assert os.path.exists(os.path.join(GLOBAL_DIR, "self", "facts"))
    assert os.path.exists(os.path.join(GLOBAL_DIR, "self", "reflections"))
    print("OK test_directory_structure")


async def test_toml_tree_store_crud():
    from KiraOS_Plugin.memory.memory_index import MemoryIndex
    from KiraOS_Plugin.memory.toml_tree_store import TomlTreeStore
    from KiraOS_Plugin.memory.memory_paths import ensure_directory_structure

    ensure_directory_structure()
    index = MemoryIndex(db_path=TEST_DB_PATH)
    store = TomlTreeStore(index=index)

    mem = await store.add_memory(
        content_text="用户喜欢 Python 编程",
        memory_type="fact",
        importance=7,
        tags=["programming", "python"],
        semantic_id="likes_python",
        entity_id="test_user_1",
        entity_type="user",
        folder="facts",
    )
    assert mem.id == "likes_python"
    assert mem.text == "用户喜欢 Python 编程"
    assert mem.importance == 7
    assert "programming" in mem.tags
    assert mem.file_path.endswith(".toml")

    with open(mem.file_path, "rb") as f:
        file_data = tomllib.load(f)
    assert "meta" not in file_data
    assert file_data["id"] == "likes_python"

    idx_meta = index.get_meta(mem.id)
    assert idx_meta is not None and idx_meta["importance"] == 7

    fetched = await store.get_memory(
        memory_id="likes_python", entity_id="test_user_1",
        entity_type="user", folder="facts",
    )
    assert fetched is not None and fetched.text == mem.text

    fetched.text = "用户喜欢 Python 和 Rust 编程"
    fetched.importance = 9
    assert await store.update_memory(fetched) is True
    assert index.get_meta(mem.id)["importance"] == 9

    all_mems = await store.get_all_memories(
        entity_id="test_user_1", entity_type="user", folder="facts",
    )
    assert len(all_mems) == 1

    assert await store.delete_memory(
        memory_id="likes_python", entity_id="test_user_1",
        entity_type="user", folder="facts",
    ) is True
    assert index.get_meta(mem.id) is None

    index.close()
    print("OK test_toml_tree_store_crud")


async def test_semantic_id_fallback():
    from KiraOS_Plugin.memory.memory_index import MemoryIndex
    from KiraOS_Plugin.memory.toml_tree_store import TomlTreeStore
    from KiraOS_Plugin.memory.memory_paths import ensure_directory_structure

    ensure_directory_structure()
    index = MemoryIndex(db_path=TEST_DB_PATH)
    store = TomlTreeStore(index=index)

    mem = await store.add_memory(
        content_text="用户养了一只猫",
        importance=4,
        entity_id="test_user_2", entity_type="user", folder="facts",
    )
    assert mem.id and "_" in mem.id
    assert mem.file_path.endswith(".toml")
    index.close()
    print("OK test_semantic_id_fallback")


async def test_fts5_search():
    from KiraOS_Plugin.memory.memory_index import MemoryIndex
    from KiraOS_Plugin.memory.toml_tree_store import TomlTreeStore
    from KiraOS_Plugin.memory.memory_paths import ensure_directory_structure

    ensure_directory_structure()
    index = MemoryIndex(db_path=TEST_DB_PATH)
    store = TomlTreeStore(index=index)

    await store.add_memory(
        content_text="用户是一名后端工程师，擅长 Python",
        importance=8, tags=["backend", "python"],
        semantic_id="backend_engineer",
        entity_id="search_user", entity_type="user", folder="facts",
    )
    await store.add_memory(
        content_text="用户讨厌写 CSS，觉得前端很烦",
        importance=6, tags=["frontend", "css"],
        semantic_id="hates_css",
        entity_id="search_user", entity_type="user", folder="facts",
    )
    await store.add_memory(
        content_text="用户养了一只叫小橘的猫",
        importance=4, tags=["pet", "cat"],
        semantic_id="pet_cat_xiaoju",
        entity_id="search_user", entity_type="user", folder="facts",
    )

    results = await store.search(
        query="Python 后端开发",
        entity_id="search_user", entity_type="user", folder="facts",
        k=2,
    )
    assert len(results) > 0
    assert "Python" in results[0].text or "后端" in results[0].text

    await store.add_memory(
        content_text="用户倾向于使用简洁的代码风格",
        importance=7, tags=["code-style"],
        semantic_id="prefers_concise_code",
        entity_id="search_user", entity_type="user", folder="reflections",
    )
    cross_results = await store.search_across_folders(
        query="代码风格",
        entity_id="search_user", entity_type="user",
        folders=["facts", "reflections"],
        k=3,
    )
    assert len(cross_results) > 0
    index.close()
    print("OK test_fts5_search")


async def test_content_hash_dedup():
    from KiraOS_Plugin.memory.memory_index import MemoryIndex

    index = MemoryIndex(db_path=TEST_DB_PATH)
    index.upsert(
        memory_id="hash_test_1",
        raw_text="用户喜欢 Python",
        entity_id="hash_user", entity_type="user", folder="facts",
    )
    content_hash = MemoryIndex.content_hash("用户喜欢 Python")
    found = index.find_by_hash(content_hash, "hash_user", "user", "facts")
    assert found is not None and found["id"] == "hash_test_1"

    diff = MemoryIndex.content_hash("用户讨厌 Python")
    assert index.find_by_hash(diff, "hash_user", "user", "facts") is None
    index.close()
    print("OK test_content_hash_dedup")


async def test_entity_profile():
    from KiraOS_Plugin.memory.entity_profile import EntityProfileStore
    from KiraOS_Plugin.memory.memory_paths import ensure_directory_structure

    ensure_directory_structure()
    store = EntityProfileStore()
    profile = await store.get_profile("profile_test_user", "user")
    assert profile.entity_id == "profile_test_user"

    await store.update_profile(
        "profile_test_user", "user",
        name="Alice", nickname="小A", platform="telegram",
    )
    updated = await store.get_profile("profile_test_user", "user")
    assert updated.name == "Alice" and updated.nickname == "小A"

    await store.add_trait("profile_test_user", "技术导向")
    await store.add_fact("profile_test_user", "喜欢 Rust 语言")
    profile = await store.get_profile("profile_test_user", "user")
    assert "技术导向" in profile.traits
    assert "喜欢 Rust 语言" in profile.facts

    prompt = await store.get_profile_prompt("profile_test_user", "user")
    assert "Alice" in prompt and "技术导向" in prompt

    await store.update_profile("test_group_1", "group", name="技术讨论组")
    group_profile = await store.get_profile("test_group_1", "group")
    assert group_profile.name == "技术讨论组"
    assert group_profile.entity_type == "group"
    print("OK test_entity_profile")


async def test_memory_decay():
    from KiraOS_Plugin.memory.memory_index import MemoryIndex
    from KiraOS_Plugin.memory.toml_tree_store import TomlTreeStore
    from KiraOS_Plugin.memory.memory_decay import MemoryDecayEngine
    from KiraOS_Plugin.memory.memory_paths import ensure_directory_structure

    ensure_directory_structure()
    index = MemoryIndex(db_path=TEST_DB_PATH)
    store = TomlTreeStore(index=index)
    engine = MemoryDecayEngine(store)

    mem = await store.add_memory(
        content_text="用户三个月前提到过一次某个话题",
        importance=2, semantic_id="old_topic_mention",
        entity_id="decay_user", entity_type="user", folder="facts",
    )

    old_time = time.time() - 90 * 86400
    index.update_meta(mem.id, last_accessed=old_time)
    index._conn.execute(
        "UPDATE memories SET timestamp = ? WHERE id = ?", (old_time, mem.id)
    )
    index._conn.commit()
    meta = index.get_meta(mem.id)
    score = engine.calculate_retention_score(meta)
    assert score < 0.4

    deleted, downgraded = await engine.garbage_collect("decay_user", "user", "facts")
    assert deleted > 0 or downgraded > 0
    index.close()
    print("OK test_memory_decay")


async def test_archive():
    from KiraOS_Plugin.memory.memory_index import MemoryIndex
    from KiraOS_Plugin.memory.toml_tree_store import TomlTreeStore
    from KiraOS_Plugin.memory.memory_paths import (
        ensure_directory_structure, ARCHIVE_DIR,
    )

    ensure_directory_structure()
    index = MemoryIndex(db_path=TEST_DB_PATH)
    store = TomlTreeStore(index=index)
    mem = await store.add_memory(
        content_text="将被归档的记忆",
        importance=3, semantic_id="to_be_archived",
        entity_id="archive_user", entity_type="user", folder="facts",
    )
    assert await store.archive_memory(
        memory_id=mem.id, entity_id="archive_user",
        entity_type="user", folder="facts",
    ) is True
    assert await store.get_memory(mem.id, "archive_user", "user", "facts") is None
    assert index.get_meta(mem.id) is None
    archive_file = os.path.join(ARCHIVE_DIR, f"{mem.id}.toml")
    assert os.path.exists(archive_file)
    with open(archive_file, "rb") as f:
        data = tomllib.load(f)
    assert "meta" in data
    index.close()
    print("OK test_archive")


async def test_global_memory():
    from KiraOS_Plugin.memory.memory_index import MemoryIndex
    from KiraOS_Plugin.memory.toml_tree_store import TomlTreeStore
    from KiraOS_Plugin.memory.memory_paths import (
        ensure_directory_structure, get_global_self_dir,
    )

    ensure_directory_structure()
    index = MemoryIndex(db_path=TEST_DB_PATH)
    store = TomlTreeStore(index=index)
    global_self = get_global_self_dir()

    mem = await store.add_memory(
        content_text="我回答用户问题时倾向于过于详细",
        memory_type="fact", importance=3, tags=["self-awareness"],
        semantic_id="verbose_answers",
        base_dir=global_self, folder="facts",
    )
    assert mem.id == "verbose_answers"

    all_self_facts = await store.get_all_memories(base_dir=global_self, folder="facts")
    assert len(all_self_facts) >= 1
    assert any("过于详细" in m.text for m in all_self_facts)
    index.close()
    print("OK test_global_memory")


async def test_index_rebuild():
    from KiraOS_Plugin.memory.memory_index import MemoryIndex
    from KiraOS_Plugin.memory.toml_tree_store import TomlTreeStore
    from KiraOS_Plugin.memory.memory_paths import (
        ensure_directory_structure, MEMORY_ROOT,
    )

    ensure_directory_structure()
    index = MemoryIndex(db_path=TEST_DB_PATH)
    store = TomlTreeStore(index=index)
    mem1 = await store.add_memory(
        content_text="重建测试记忆 1", importance=7,
        semantic_id="rebuild_test_1",
        entity_id="rebuild_user", entity_type="user", folder="facts",
    )
    mem2 = await store.add_memory(
        content_text="重建测试记忆 2", importance=5,
        semantic_id="rebuild_test_2",
        entity_id="rebuild_user", entity_type="user", folder="facts",
    )

    index._conn.execute("DELETE FROM memories")
    index._conn.execute("DELETE FROM memories_fts")
    index._conn.commit()
    assert index.get_meta(mem1.id) is None

    index.rebuild_index_from_files(MEMORY_ROOT)
    rebuilt1 = index.get_meta(mem1.id)
    rebuilt2 = index.get_meta(mem2.id)
    assert rebuilt1 is not None and rebuilt2 is not None
    assert rebuilt1["importance"] == 7
    index.close()
    print("OK test_index_rebuild")


async def test_legacy_migration():
    """Build a fake legacy kiraos.db and ensure migrate.py converts each row."""
    from KiraOS_Plugin.memory.memory_index import MemoryIndex
    from KiraOS_Plugin.memory.toml_tree_store import TomlTreeStore
    from KiraOS_Plugin.memory.entity_profile import EntityProfileStore
    from KiraOS_Plugin.memory.memory_extractor import MemoryExtractor
    from KiraOS_Plugin.memory.memory_decay import MemoryDecayEngine
    from KiraOS_Plugin.memory.memory_manager import MemoryManager
    from KiraOS_Plugin.memory.memory_paths import (
        ensure_directory_structure, MEMORY_ROOT,
    )
    from KiraOS_Plugin import migrate as legacy_migrate

    ensure_directory_structure()
    legacy_db_path = os.path.join(MEMORY_ROOT, "kiraos.db")

    conn = sqlite3.connect(legacy_db_path)
    conn.executescript("""
        CREATE TABLE user_profiles (
            user_id TEXT NOT NULL, memory_key TEXT NOT NULL,
            memory_value TEXT NOT NULL,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            confidence REAL DEFAULT 0.5,
            category TEXT DEFAULT 'basic',
            expires_at INTEGER DEFAULT NULL,
            PRIMARY KEY (user_id, memory_key)
        );
        CREATE TABLE event_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            event_summary TEXT NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            tag TEXT DEFAULT NULL
        );
    """)
    conn.execute(
        "INSERT INTO user_profiles (user_id, memory_key, memory_value, confidence, category) "
        "VALUES (?, ?, ?, ?, ?)",
        ("user1", "昵称", "小明", 0.9, "basic"),
    )
    conn.execute(
        "INSERT INTO user_profiles (user_id, memory_key, memory_value, confidence, category) "
        "VALUES (?, ?, ?, ?, ?)",
        ("user1", "城市", "北京", 0.8, "basic"),
    )
    conn.execute(
        "INSERT INTO event_logs (user_id, event_summary, tag) VALUES (?, ?, ?)",
        ("user1", "完成半马跑步", "milestone"),
    )
    conn.commit()
    conn.close()

    index = MemoryIndex(db_path=TEST_DB_PATH)
    tree_store = TomlTreeStore(index=index)
    profile_store = EntityProfileStore()
    extractor = MemoryExtractor(tree_store, llm_client=None)
    decay_engine = MemoryDecayEngine(tree_store)
    manager = MemoryManager(
        index=index, tree_store=tree_store, profile_store=profile_store,
        extractor=extractor, decay_engine=decay_engine,
    )

    stats = await legacy_migrate.migrate_legacy_db_if_needed(manager, legacy_db_path)
    assert stats["status"] == "migrated", stats
    assert stats["profiles"] == 2, stats
    assert stats["events"] == 1, stats

    user_facts = await tree_store.get_all_memories(
        entity_id="user1", entity_type="user", folder="facts",
    )
    texts = [m.text for m in user_facts]
    assert any("昵称: 小明" in t for t in texts), texts
    assert any("城市: 北京" in t for t in texts), texts
    assert any("半马" in t for t in texts), texts

    # Sentinel and backup
    assert os.path.exists(os.path.join(MEMORY_ROOT, ".migrated_v3"))
    assert os.path.exists(legacy_db_path + ".legacy.bak")
    assert not os.path.exists(legacy_db_path)

    # Second call must be a no-op.
    stats2 = await legacy_migrate.migrate_legacy_db_if_needed(manager, legacy_db_path)
    assert stats2["status"] == "skipped_sentinel"

    index.close()
    print("OK test_legacy_migration")


async def main():
    setup_test_env()
    try:
        await test_directory_structure()
        await test_toml_tree_store_crud()
        await test_semantic_id_fallback()
        await test_fts5_search()
        await test_content_hash_dedup()
        await test_entity_profile()
        await test_memory_decay()
        await test_archive()
        await test_global_memory()
        await test_index_rebuild()
        await test_legacy_migration()
        print("\nAll 11 tests passed.")
    finally:
        teardown_test_env()


if __name__ == "__main__":
    asyncio.run(main())
