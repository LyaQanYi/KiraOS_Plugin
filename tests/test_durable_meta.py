"""Durable runtime metadata across index rebuilds.

`MemoryManager.async_init()` truncates the SQLite index and rebuilds it from
TOML on *every* startup (memory_manager.py:117-125 -> memory_index.py
rebuild_index_from_files, which runs `DELETE FROM memories`). Before this was
fixed, `to_toml_dict()` deliberately omitted runtime metadata, so every
restart reset `timestamp` to `time.time()` and `access_count` to 0 — which
made age-based forgetting ("archive memories unaccessed for 30 days")
permanently unreachable, and made every memory look brand new.

These tests pin the two halves of the fix:
  1. TOML carries `[meta]`, so the data survives even if the index DB is lost.
  2. The rebuild snapshots live SQLite meta first, so accesses recorded
     between TOML writes are not discarded.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from plugins.KiraOS_Plugin.memory.memory_paths import set_data_root
from plugins.KiraOS_Plugin.memory.memory_index import MemoryIndex
from plugins.KiraOS_Plugin.memory.toml_tree_store import Memory, TomlTreeStore


@pytest.fixture
def store(tmp_path):
    """A TomlTreeStore rooted at a throwaway directory."""
    set_data_root(tmp_path / "memory")
    index = MemoryIndex()
    yield TomlTreeStore(index=index)
    index.close()


# The index is keyed on the composite PK (entity_type, entity_id, folder,
# base_dir, id), so every lookup needs the full entity context.
def _key(mem_id, entity_id, entity_type="user", folder="facts"):
    return dict(
        entity_id=entity_id, entity_type=entity_type, folder=folder, base_dir=""
    )


def _read_toml(path):
    try:
        import tomllib
    except ImportError:  # Python 3.10
        import tomli as tomllib
    with open(path, "rb") as f:
        return tomllib.load(f)


# ── 1. The writer emits [meta] ───────────────────────────────────────────────

def test_to_toml_dict_includes_durable_meta():
    mem = Memory(
        id="likes_tea", type="fact", text="喜欢喝茶",
        meta={"timestamp": 1000.0, "last_accessed": 2000.0, "access_count": 7},
    )
    d = mem.to_toml_dict()
    assert d["meta"] == {
        "timestamp": 1000.0, "last_accessed": 2000.0, "access_count": 7
    }


def test_to_toml_dict_omits_content_fields_from_meta():
    """`meta` also carries importance/tags/source, which already have an
    authoritative copy at the TOML top level. Writing them twice invites a
    hand-editor to change one and desync the other."""
    mem = Memory(
        id="x", type="fact", text="t", importance=9, tags=["a"],
        meta={
            "timestamp": 1.0, "access_count": 0, "last_accessed": 1.0,
            "importance": 3, "tags": ["stale"], "source": {"s": 1},
        },
    )
    d = mem.to_toml_dict()
    assert set(d["meta"]) == {"timestamp", "last_accessed", "access_count"}
    assert d["importance"] == 9
    assert d["tags"] == ["a"]


def test_durable_meta_coerces_bad_types():
    """meta can come from hand-edited TOML or legacy JSON. tomli_w raises on
    None and would fail the whole write, so junk is dropped, not propagated."""
    mem = Memory(
        id="x", type="fact", text="t",
        meta={"timestamp": "1500.5", "last_accessed": None, "access_count": "3"},
    )
    meta = mem.to_toml_dict()["meta"]
    assert meta["timestamp"] == 1500.5
    assert meta["access_count"] == 3
    assert "last_accessed" not in meta


def test_memory_with_no_meta_writes_no_meta_table():
    mem = Memory(id="x", type="fact", text="t")
    assert "meta" not in mem.to_toml_dict()


# ── 2. The reader recovers [meta] without SQLite ─────────────────────────────

def test_from_toml_dict_falls_back_to_embedded_meta():
    data = {
        "id": "x", "type": "fact", "text": "t",
        "meta": {"timestamp": 500.0, "access_count": 4, "last_accessed": 600.0},
    }
    mem = Memory.from_toml_dict(data)
    assert mem.timestamp == 500.0
    assert mem.access_count == 4
    assert mem.last_accessed == 600.0


def test_runtime_meta_takes_precedence_over_embedded():
    """SQLite is live-updated on every recall; the TOML copy is only as fresh
    as the last write."""
    data = {
        "id": "x", "type": "fact", "text": "t",
        "meta": {"timestamp": 500.0, "access_count": 4},
    }
    mem = Memory.from_toml_dict(data, runtime_meta={"timestamp": 500.0, "access_count": 99})
    assert mem.access_count == 99


# ── 3. End-to-end: metadata survives a simulated restart ─────────────────────

def test_creation_timestamp_survives_restart(store, tmp_path):
    """The regression that made age-based decay unreachable."""
    async def scenario():
        mem = await store.add_memory(
            content_text="用户住在上海",
            semantic_id="lives_in_shanghai",
            entity_id="onebot:12345",
            entity_type="user",
        )
        return mem

    mem = asyncio.run(scenario())
    original_ts = mem.timestamp
    assert original_ts > 0

    # The TOML on disk must carry it, or a lost index DB loses the age.
    on_disk = _read_toml(mem.file_path)
    assert on_disk["meta"]["timestamp"] == pytest.approx(original_ts)

    # Simulate a restart: brand new index, rebuilt from TOML only.
    from plugins.KiraOS_Plugin.memory.memory_paths import get_memory_root
    fresh = MemoryIndex()
    fresh.rebuild_index_from_files(get_memory_root())

    row = fresh.get_meta("lives_in_shanghai", **_key("lives_in_shanghai", "onebot:12345"))
    assert row is not None, "memory vanished from the rebuilt index"
    assert row["timestamp"] == pytest.approx(original_ts, abs=0.01), (
        "creation timestamp was reset by the rebuild — age-based decay is dead"
    )
    fresh.close()


def test_access_count_survives_restart(store):
    """Accesses recorded in SQLite between TOML writes must not be discarded
    by the startup rebuild."""
    async def scenario():
        return await store.add_memory(
            content_text="用户喜欢猫",
            semantic_id="likes_cats",
            entity_id="onebot:12345",
            entity_type="user",
        )

    asyncio.run(scenario())
    k = _key("likes_cats", "onebot:12345")

    # Three recalls bump the counter in SQLite only.
    for _ in range(3):
        store.index.touch_access("likes_cats", **k)
    assert store.index.get_meta("likes_cats", **k)["access_count"] == 3

    from plugins.KiraOS_Plugin.memory.memory_paths import get_memory_root
    fresh = MemoryIndex()
    fresh.rebuild_index_from_files(get_memory_root())

    row = fresh.get_meta("likes_cats", **k)
    assert row["access_count"] == 3, (
        "access count reset by rebuild — the SQLite snapshot was not applied"
    )
    fresh.close()


def test_rebuild_is_monotonic_for_access_count(store):
    """Rebuilding repeatedly must never walk the counter backwards, even
    though TOML's copy is stale relative to SQLite."""
    async def scenario():
        return await store.add_memory(
            content_text="用户是程序员",
            semantic_id="is_dev",
            entity_id="onebot:999",
            entity_type="user",
        )

    asyncio.run(scenario())
    from plugins.KiraOS_Plugin.memory.memory_paths import get_memory_root
    root = get_memory_root()
    k = _key("is_dev", "onebot:999")

    for _ in range(5):
        store.index.touch_access("is_dev", **k)

    counts = []
    for _ in range(3):
        store.index.rebuild_index_from_files(root)
        counts.append(store.index.get_meta("is_dev", **k)["access_count"])

    assert counts == [5, 5, 5], f"access_count drifted across rebuilds: {counts}"


def test_rebuild_without_prior_index_uses_toml_meta(store):
    """Disaster recovery: the index DB is gone, TOML is all that is left."""
    async def scenario():
        return await store.add_memory(
            content_text="用户在北京工作",
            semantic_id="works_in_beijing",
            entity_id="onebot:777",
            entity_type="user",
        )

    mem = asyncio.run(scenario())
    original_ts = mem.timestamp

    from plugins.KiraOS_Plugin.memory.memory_paths import get_memory_root
    import os
    db_path = store.index.db_path
    store.index.close()
    if os.path.exists(db_path):
        os.remove(db_path)

    fresh = MemoryIndex()
    fresh.rebuild_index_from_files(get_memory_root())
    row = fresh.get_meta("works_in_beijing", **_key("works_in_beijing", "onebot:777"))
    assert row is not None
    assert row["timestamp"] == pytest.approx(original_ts, abs=0.01), (
        "TOML [meta] did not carry the creation time through a lost index"
    )
    fresh.close()


# ── 4. The payoff: age-based forgetting can actually fire ────────────────────

def test_aged_memory_stays_aged_across_restart(store):
    """The reason durable metadata matters.

    `MemoryDecayEngine` archives a memory once its retention score drops below
    THRESHOLD_DELETE, and that score is driven by `days_since_creation` and
    `days_since_access`. When the startup rebuild reset both clocks to now,
    every memory looked freshly created on every boot and the score never fell
    — so "forget what hasn't been touched in 30 days" could never trigger on a
    bot that gets restarted.
    """
    from plugins.KiraOS_Plugin.memory.memory_decay import MemoryDecayEngine
    from plugins.KiraOS_Plugin.memory.memory_paths import get_memory_root

    entity = "onebot:555"
    k = _key("old_trivia", entity)

    async def scenario():
        return await store.add_memory(
            content_text="用户上周随口提过一句天气",
            semantic_id="old_trivia",
            importance=1,
            entity_id=entity,
            entity_type="user",
        )

    asyncio.run(scenario())

    # Backdate it a year, both in the index and on disk, as if it had been
    # sitting untouched since then.
    long_ago = time.time() - 365 * 86400

    async def backdate():
        mem = await store.get_memory(
            memory_id="old_trivia", entity_id=entity,
            entity_type="user", folder="facts",
        )
        mem.meta["timestamp"] = long_ago
        mem.meta["last_accessed"] = long_ago
        await store.update_memory(mem)

    asyncio.run(backdate())
    with store.index._transaction() as cur:
        cur.execute(
            "UPDATE memories SET timestamp = ?, last_accessed = ? WHERE id = ?",
            (long_ago, long_ago, "old_trivia"),
        )

    score_before = MemoryDecayEngine.calculate_retention_score(
        store.index.get_meta("old_trivia", **k)
    )

    # Restart.
    store.index.rebuild_index_from_files(get_memory_root())

    meta_after = store.index.get_meta("old_trivia", **k)
    age_days = (time.time() - meta_after["timestamp"]) / 86400
    assert age_days > 300, (
        f"restart reset the memory's age to {age_days:.1f} days — "
        "decay clocks are still being wiped"
    )

    score_after = MemoryDecayEngine.calculate_retention_score(meta_after)
    assert score_after == pytest.approx(score_before, abs=0.01), (
        "retention score jumped across a restart"
    )
    assert score_after < MemoryDecayEngine.THRESHOLD_DELETE, (
        f"a year-old, never-accessed, importance-1 memory scored {score_after:.3f}, "
        f"which is above the {MemoryDecayEngine.THRESHOLD_DELETE} archive threshold"
    )
