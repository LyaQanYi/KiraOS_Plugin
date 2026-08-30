"""Storage-layer tests, ported from the hippocampus plugin's suite.

Source: kira_plugin_hippocampus_memory/tests/test_memory_system.py — the
sections covering path management, TomlTreeStore CRUD, content-hash dedup,
entity profiles, and the decay engine. Adapted to KiraOS's API surface:

  * `set_memory_root()`      -> `set_data_root()`
  * `memory.paths`           -> `memory.memory_paths`
  * `HippocampusManager`     -> `MemoryManager`
  * `increment_interaction(nickname=...)` takes the nickname through
    `**extra_updates` here rather than as a declared parameter.

The rest of that suite (recall-query derivation, sender attribution, profile
compaction, persona perspective, entity search) covers code that does not
exist in KiraOS yet; those tests come over with the PRs that port their
subjects, so they gate real behaviour instead of being commented out.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest

from plugins.KiraOS_Plugin.memory import memory_paths
from plugins.KiraOS_Plugin.memory.memory_paths import (
    set_data_root,
    ensure_directory_structure,
    get_index_db_path,
)
from plugins.KiraOS_Plugin.memory.memory_index import MemoryIndex
from plugins.KiraOS_Plugin.memory.toml_tree_store import TomlTreeStore
from plugins.KiraOS_Plugin.memory.entity_profile import (
    EntityProfile,
    EntityProfileStore,
)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


@pytest.fixture
def rooted(tmp_path):
    """Point the module-global data root at a throwaway directory."""
    set_data_root(tmp_path / "memory")
    ensure_directory_structure()
    return tmp_path / "memory"


@pytest.fixture
def store(rooted):
    index = MemoryIndex(db_path=get_index_db_path())
    s = TomlTreeStore(index=index)
    yield s
    index.close()


# ── Path management ──────────────────────────────────────────────────────────

def test_path_accessors_follow_the_injected_root(tmp_path):
    root = tmp_path / "memory"
    set_data_root(root)
    assert memory_paths.get_memory_root() == str(root)
    assert memory_paths.get_entities_dir().endswith("entities")
    assert memory_paths.get_global_dir().endswith("global")
    assert memory_paths.get_archive_dir().endswith("archive")
    assert memory_paths.get_index_db_path().endswith("memory_index.db")


def test_directory_structure(rooted):
    for sub in (
        "entities", "archive", "global", "global/facts",
        "global/self/facts", "global/self/reflections",
    ):
        assert (rooted / sub).exists(), f"missing {sub}"


# ── TomlTreeStore CRUD ───────────────────────────────────────────────────────

def test_toml_store_crud(store):
    async def run():
        mem = await store.add_memory(
            content_text="用户喜欢 Python",
            memory_type="fact",
            importance=7,
            tags=["preference"],
            entity_id="telegram:42",
            entity_type="user",
            folder="facts",
        )
        assert mem.id
        assert mem.text == "用户喜欢 Python"

        fpath = mem.file_path
        assert Path(fpath).exists()

        got = await store.get_memory(
            mem.id, entity_id="telegram:42", entity_type="user", folder="facts"
        )
        assert got is not None
        assert got.text == "用户喜欢 Python"

        hits = await store.search(
            query="Python", entity_id="telegram:42", entity_type="user",
            folder="facts", k=5,
        )
        assert any(h.id == mem.id for h in hits)

        cross = await store.search_across_folders(
            query="Python", entity_id="telegram:42", entity_type="user", k=5,
        )
        assert any(h.id == mem.id for h in cross)

        ok = await store.delete_memory(
            mem.id, entity_id="telegram:42", entity_type="user", folder="facts"
        )
        assert ok
        assert not Path(fpath).exists()

    _run(run())


def test_entity_id_with_colon_round_trips_through_the_filesystem(store):
    """`adapter:id` is the canonical entity form and must survive path
    encoding — colons are reserved on Windows NTFS."""
    async def run():
        mem = await store.add_memory(
            content_text="带冒号的实体 id",
            entity_id="onebot:12345",
            entity_type="user",
            folder="facts",
        )
        assert "%3A" in mem.file_path, "colon was not percent-encoded"

        got = await store.get_memory(
            mem.id, entity_id="onebot:12345", entity_type="user", folder="facts"
        )
        assert got is not None and got.text == "带冒号的实体 id"

        found = memory_paths.list_all_entities()
        assert ("onebot:12345", "user") in found, (
            f"entity id did not decode back from the directory name: {found}"
        )

    _run(run())


# ── Content-hash dedup ───────────────────────────────────────────────────────

def test_content_hash_dedup(store):
    async def run():
        content = "完全相同的事实"
        await store.add_memory(
            content_text=content,
            memory_type="fact",
            importance=5,
            entity_id="user42",
            entity_type="user",
            folder="facts",
        )
        h1 = MemoryIndex.content_hash(content)
        found = store.index.find_by_hash(h1, "user42", "user", "facts")
        assert found is not None
        assert found["raw_text"] == content

    _run(run())


def test_update_memory_survives_null_tags_from_llm(store):
    """A null in the tags array must not break the merge write path.

    The hippocampus merge path unions LLM-extracted tags into the matched
    memory. An LLM that emits `null` inside that array used to blow up both
    writers -- tomli_w on the TOML side, `" ".join` on the index side -- so
    update_memory returned False and the merge was silently lost.
    """
    async def run():
        mem = await store.add_memory(
            content_text="周武住在杭州",
            memory_type="fact",
            importance=6,
            tags=["location"],
            entity_id="qq:769690776",
            entity_type="user",
            folder="facts",
        )

        mem.text = "周武住在杭州，最近搬到了西湖区"
        mem.tags = ["location", None, "", "city"]

        assert await store.update_memory(mem) is True

        reread = await store.get_memory(
            mem.id, "qq:769690776", "user", "facts"
        )
        assert reread is not None
        assert reread.text == "周武住在杭州，最近搬到了西湖区"
        assert None not in reread.tags

    _run(run())


def test_content_hash_is_namespaced_per_entity(store):
    """The same sentence about two different people is two memories."""
    async def run():
        content = "喜欢喝咖啡"
        for eid in ("onebot:1", "onebot:2"):
            await store.add_memory(
                content_text=content, entity_id=eid,
                entity_type="user", folder="facts",
            )
        h = MemoryIndex.content_hash(content)
        assert store.index.find_by_hash(h, "onebot:1", "user", "facts") is not None
        assert store.index.find_by_hash(h, "onebot:2", "user", "facts") is not None
        assert store.index.find_by_hash(h, "onebot:3", "user", "facts") is None

    _run(run())


# ── Entity profile ───────────────────────────────────────────────────────────

def test_entity_profile(rooted):
    async def run():
        ps = EntityProfileStore()

        p = await ps.get_profile("user42", "user")
        assert isinstance(p, EntityProfile)
        assert p.entity_id == "user42"

        await ps.add_trait("user42", "技术导向")
        await ps.add_fact("user42", "喜欢 Python")
        await ps.increment_interaction("user42", nickname="小明")

        p2 = await ps.get_profile("user42", "user")
        assert "技术导向" in p2.traits
        assert "喜欢 Python" in p2.facts
        assert p2.nickname == "小明"
        assert p2.interaction_count == 1

        # A nickname change archives the old one into aliases.
        await ps.increment_interaction("user42", nickname="小红")
        p3 = await ps.get_profile("user42", "user")
        assert "小明" in p3.aliases
        assert p3.nickname == "小红"

    _run(run())


def test_profile_survives_a_reload(rooted):
    """profile.json is the source of truth; a fresh store must see it."""
    async def run():
        await EntityProfileStore().add_fact("onebot:7", "住在深圳")
        reloaded = await EntityProfileStore().get_profile("onebot:7", "user")
        assert "住在深圳" in reloaded.facts

    _run(run())


def test_concurrent_profile_updates_do_not_lose_writes(rooted):
    """The per-entity lock exists because get→mutate→save is a RMW cycle;
    without it concurrent appends clobber each other."""
    async def run():
        ps = EntityProfileStore()
        await asyncio.gather(*[
            ps.add_fact("onebot:9", f"事实{i}") for i in range(10)
        ])
        p = await ps.get_profile("onebot:9", "user")
        assert len(p.facts) == 10, f"lost writes: kept {len(p.facts)}/10"

    _run(run())


# ── Decay engine ─────────────────────────────────────────────────────────────

def test_retention_score_falls_with_age_and_disuse():
    from plugins.KiraOS_Plugin.memory.memory_decay import MemoryDecayEngine
    import time

    now = time.time()
    fresh = MemoryDecayEngine.calculate_retention_score(
        {"importance": 5, "access_count": 0,
         "timestamp": now, "last_accessed": now},
        now,
    )
    stale = MemoryDecayEngine.calculate_retention_score(
        {"importance": 5, "access_count": 0,
         "timestamp": now - 365 * 86400, "last_accessed": now - 365 * 86400},
        now,
    )
    assert stale < fresh


def test_frequently_accessed_memories_score_higher():
    from plugins.KiraOS_Plugin.memory.memory_decay import MemoryDecayEngine
    import time

    now = time.time()
    base = {"importance": 5, "timestamp": now - 30 * 86400,
            "last_accessed": now - 30 * 86400}
    rare = MemoryDecayEngine.calculate_retention_score({**base, "access_count": 0}, now)
    often = MemoryDecayEngine.calculate_retention_score({**base, "access_count": 20}, now)
    assert often > rare


def test_reflections_outrank_facts_at_equal_age():
    """Reflections are distilled from many facts, so they decay slower."""
    from plugins.KiraOS_Plugin.memory.memory_decay import MemoryDecayEngine
    import time

    now = time.time()
    base = {"importance": 5, "access_count": 0,
            "timestamp": now - 60 * 86400, "last_accessed": now - 60 * 86400}
    fact = MemoryDecayEngine.calculate_retention_score(
        {**base, "memory_type": "fact"}, now
    )
    reflection = MemoryDecayEngine.calculate_retention_score(
        {**base, "memory_type": "reflection"}, now
    )
    assert reflection > fact


def test_decay_downgrade_and_archive(store):
    """A year-old, unimportant, never-accessed fact gets archived."""
    from plugins.KiraOS_Plugin.memory.memory_decay import MemoryDecayEngine
    import time

    async def run():
        engine = MemoryDecayEngine(store)
        mem = await store.add_memory(
            content_text="一条无关紧要的旧事实",
            importance=1,
            entity_id="onebot:55",
            entity_type="user",
            folder="facts",
        )
        long_ago = time.time() - 365 * 86400
        with store.index._transaction() as cur:
            cur.execute(
                "UPDATE memories SET timestamp = ?, last_accessed = ? WHERE id = ?",
                (long_ago, long_ago, mem.id),
            )

        deleted, downgraded = await engine.garbage_collect(
            entity_id="onebot:55", entity_type="user", folder="facts"
        )
        assert deleted + downgraded >= 1, "aged low-value memory was untouched"
        assert not Path(mem.file_path).exists(), "archived file left in place"

    _run(run())
