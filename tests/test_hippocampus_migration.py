"""End-to-end tests for the hippocampus → KiraOS one-shot migration.

The migration is the only code path that touches *existing user data*, so it is
the one place where a silent failure costs someone their memories. These tests
build a realistic hippocampus data tree on disk — TOML facts/reflections, a
`profile.json`, and a populated `memory_index.db` using hippocampus's *own*
schema — then run the migration against it and assert on what landed.

The source fixtures deliberately mirror hippocampus exactly:
- SQLite `memories` table with `id TEXT PRIMARY KEY` and **no** `storage_key`
  column (see kira_plugin_hippocampus_memory/memory/memory_index.py:65-81)
- entity dirs named `{type}_{quote(id)}` (paths.py:99), so a colon-bearing id
  like `telegram:12345` arrives percent-encoded as `telegram%3A12345`
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time

import pytest

from plugins.KiraOS_Plugin.memory.memory_paths import set_data_root
from plugins.KiraOS_Plugin.memory.memory_index import MemoryIndex
from plugins.KiraOS_Plugin.memory.toml_tree_store import TomlTreeStore
from plugins.KiraOS_Plugin.memory.entity_profile import EntityProfileStore


# ── Source-tree builders (hippocampus format) ────────────────────────────────

# Hippocampus's real schema. Kept verbatim rather than importing the plugin so
# these tests stay hermetic and keep passing after that repo is archived.
_HIPPO_SCHEMA = """
CREATE TABLE memories (
    id TEXT PRIMARY KEY,
    entity_id TEXT NOT NULL DEFAULT '',
    entity_type TEXT NOT NULL DEFAULT '',
    folder TEXT NOT NULL DEFAULT 'facts',
    memory_type TEXT NOT NULL DEFAULT 'fact',
    importance INTEGER NOT NULL DEFAULT 5,
    timestamp REAL NOT NULL,
    last_accessed REAL NOT NULL,
    access_count INTEGER NOT NULL DEFAULT 0,
    tags TEXT NOT NULL DEFAULT '[]',
    source TEXT NOT NULL DEFAULT '{}',
    content_hash TEXT NOT NULL DEFAULT '',
    file_path TEXT NOT NULL DEFAULT '',
    base_dir TEXT NOT NULL DEFAULT '',
    raw_text TEXT NOT NULL DEFAULT ''
)
"""


def _write_toml(path, *, mem_id, mem_type, text, importance, tags):
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f'id = "{mem_id}"',
        f'type = "{mem_type}"',
        f'text = "{text}"',
        f"importance = {importance}",
        f"tags = {json.dumps(tags)}",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _seed_hippocampus(data_root, *, entity_dir_name, entity_id, entity_type):
    """Lay out a hippocampus data tree and return its memory root."""
    hippo = data_root / "plugin_data" / "kira_plugin_hippocampus_memory" / "memory"
    ent = hippo / "entities" / entity_dir_name

    _write_toml(
        ent / "facts" / "hates_css.toml",
        mem_id="hates_css",
        mem_type="fact",
        text="用户讨厌写 CSS",
        importance=6,
        tags=["frontend", "preference"],
    )
    _write_toml(
        ent / "reflections" / "prefers_backend.toml",
        mem_id="prefers_backend",
        mem_type="reflection",
        text="用户更偏好后端工作",
        importance=7,
        tags=["work"],
    )
    _write_toml(
        hippo / "global" / "facts" / "kira_likes_tea.toml",
        mem_id="kira_likes_tea",
        mem_type="fact",
        text="Kira 喜欢喝茶",
        importance=4,
        tags=["self"],
    )

    (ent / "profile.json").write_text(
        json.dumps(
            {
                "entity_id": entity_id,
                "entity_type": entity_type,
                "name": "阿柚",
                "nickname": "柚子",
                "platform": "telegram",
                "traits": ["耐心", "技术导向"],
                "preferences": {"language": "zh"},
                "facts": ["是后端开发"],
                "aliases": ["柚"],
                "interaction_count": 42,
                "last_interaction": 1_700_000_000.0,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    # Aged runtime meta: created ~30d ago, accessed a lot. This is precisely the
    # data that a broken migration silently resets to (now, 0).
    created = time.time() - 30 * 86400
    conn = sqlite3.connect(str(hippo / "memory_index.db"))
    conn.execute(_HIPPO_SCHEMA)
    conn.executemany(
        "INSERT INTO memories (id, entity_id, entity_type, folder, memory_type,"
        " importance, timestamp, last_accessed, access_count, raw_text)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        [
            ("hates_css", entity_id, entity_type, "facts", "fact", 6,
             created, created + 86400, 17, "用户讨厌写 CSS"),
            ("prefers_backend", entity_id, entity_type, "reflections",
             "reflection", 7, created, created + 3600, 5, "用户更偏好后端工作"),
        ],
    )
    conn.commit()
    conn.close()
    return hippo


# ── Fixtures ─────────────────────────────────────────────────────────────────

_ENTITY_ID = "telegram:12345"
_ENTITY_TYPE = "user"
# What hippocampus actually names the dir: quote("telegram:12345", safe="")
_ENTITY_DIR = "user_telegram%3A12345"


@pytest.fixture
def migration_env(data_root):
    """A seeded hippocampus tree plus a live KiraOS destination store."""
    hippo_root = _seed_hippocampus(
        data_root,
        entity_dir_name=_ENTITY_DIR,
        entity_id=_ENTITY_ID,
        entity_type=_ENTITY_TYPE,
    )
    set_data_root(data_root / "memory")
    index = MemoryIndex()
    store = TomlTreeStore(index=index)
    try:
        yield {
            "data_root": data_root,
            "hippo_root": hippo_root,
            "store": store,
            "index": index,
            "profiles": EntityProfileStore(),
        }
    finally:
        index.close()


class _StubPluginMgr:
    def __init__(self):
        self.enabled = {"kira_plugin_hippocampus_memory": True}
        self.calls = []

    def is_plugin_enabled(self, pid):
        return self.enabled.get(pid, False)

    async def set_plugin_enabled(self, pid, value):
        self.calls.append((pid, value))
        self.enabled[pid] = value


class _StubCtx:
    def __init__(self):
        self.plugin_mgr = _StubPluginMgr()


async def _run(env):
    from plugins.KiraOS_Plugin.memory.migration_hippocampus import (
        migrate_hippocampus_if_needed,
    )

    ctx = _StubCtx()
    result = await migrate_hippocampus_if_needed(env["store"], env["profiles"], ctx)
    return result, ctx


# ── Tests ────────────────────────────────────────────────────────────────────


async def _co_test_migration_reports_every_memory_it_should_have_moved(migration_env):
    """The seeded tree holds 1 fact, 1 reflection, 1 global fact, 1 profile."""
    result, _ = await _run(migration_env)

    assert result["migrated"] is True
    assert result["entities"] == 1, "profile.json under the entity dir was not seen"
    assert result["facts"] == 1
    assert result["reflections"] == 1
    assert result["global_facts"] == 1


async def _co_test_migrated_memories_are_readable_through_the_normal_api(migration_env):
    """Migration output must be reachable via the same API the plugin uses."""
    await _run(migration_env)
    store = migration_env["store"]

    facts = await store.get_all_memories(_ENTITY_ID, _ENTITY_TYPE, "facts")
    texts = {m.text for m in facts}
    assert "用户讨厌写 CSS" in texts, f"fact not retrievable; got {texts}"

    reflections = await store.get_all_memories(_ENTITY_ID, _ENTITY_TYPE, "reflections")
    assert "用户更偏好后端工作" in {m.text for m in reflections}


async def _co_test_aged_runtime_meta_survives_migration(migration_env):
    """A 30-day-old memory with 17 accesses must not arrive as brand new.

    This is the decay-relevant assertion: if `timestamp`/`access_count` reset,
    every migrated memory looks freshly created and forever-unused, so
    age-based forgetting never fires and retention scoring is wrong.
    """
    await _run(migration_env)

    facts = await migration_env["store"].get_all_memories(_ENTITY_ID, _ENTITY_TYPE, "facts")
    mem = next((m for m in facts if m.id == "hates_css"), None)
    assert mem is not None, "hates_css did not survive migration"

    age_days = (time.time() - mem.timestamp) / 86400
    assert age_days > 25, (
        f"creation timestamp was reset — memory reads as {age_days:.1f} days "
        "old, expected ~30"
    )
    assert mem.access_count == 17, (
        f"access_count reset to {mem.access_count}, expected 17"
    )


async def _co_test_profile_fields_carry_over(migration_env):
    await _run(migration_env)

    profile = await migration_env["profiles"].get_profile(_ENTITY_ID, _ENTITY_TYPE)
    assert profile.name == "阿柚"
    assert profile.interaction_count == 42
    assert "耐心" in profile.traits
    assert "是后端开发" in profile.facts


async def _co_test_migration_is_idempotent(migration_env):
    """Re-running must not duplicate anything — the marker gates the second run."""
    first, _ = await _run(migration_env)
    assert first["migrated"] is True

    second, _ = await _run(migration_env)
    assert second["migrated"] is False, "marker did not gate the re-run"

    facts = await migration_env["store"].get_all_memories(_ENTITY_ID, _ENTITY_TYPE, "facts")
    assert len([m for m in facts if m.id == "hates_css"]) == 1


async def _co_test_source_data_is_never_modified(migration_env):
    """Migration is read-only on hippocampus data so rollback stays possible."""
    hippo = migration_env["hippo_root"]
    before = {
        p: p.read_bytes()
        for p in hippo.rglob("*")
        if p.is_file() and p.suffix in {".toml", ".json"}
    }
    assert before, "fixture produced no source files"

    await _run(migration_env)

    for path, content in before.items():
        assert path.exists(), f"migration deleted source file {path}"
        assert path.read_bytes() == content, f"migration mutated source {path}"


async def _co_test_hippocampus_plugin_is_disabled_after_migration(migration_env):
    """Leaving both plugins live would double-feed every incoming message."""
    _, ctx = await _run(migration_env)
    assert ("kira_plugin_hippocampus_memory", False) in ctx.plugin_mgr.calls


async def _co_test_malformed_toml_does_not_abort_the_run(migration_env):
    """One corrupt file must cost one memory, not the whole migration."""
    bad = (
        migration_env["hippo_root"]
        / "entities" / _ENTITY_DIR / "facts" / "broken.toml"
    )
    bad.write_text('id = "broken"\ntext = "unterminated', encoding="utf-8")

    result, _ = await _run(migration_env)

    assert result["migrated"] is True
    assert result["facts"] >= 1, "a single bad file took the good ones with it"
    facts = await migration_env["store"].get_all_memories(_ENTITY_ID, _ENTITY_TYPE, "facts")
    assert "用户讨厌写 CSS" in {m.text for m in facts}


async def _co_test_no_hippocampus_data_is_a_clean_noop(data_root):
    """Fresh installs must not error, and must drop the marker to skip rechecks."""
    set_data_root(data_root / "memory")
    index = MemoryIndex()
    try:
        store = TomlTreeStore(index=index)
        from plugins.KiraOS_Plugin.memory.migration_hippocampus import (
            migrate_hippocampus_if_needed,
        )

        result = await migrate_hippocampus_if_needed(
            store, EntityProfileStore(), _StubCtx()
        )
        assert result["migrated"] is False
        assert (data_root / "memory" / ".hippocampus_migrated").exists()
    finally:
        index.close()


def test_migration_reports_every_memory_it_should_have_moved(migration_env):
    asyncio.run(_co_test_migration_reports_every_memory_it_should_have_moved(migration_env))


def test_migrated_memories_are_readable_through_the_normal_api(migration_env):
    asyncio.run(_co_test_migrated_memories_are_readable_through_the_normal_api(migration_env))


def test_aged_runtime_meta_survives_migration(migration_env):
    asyncio.run(_co_test_aged_runtime_meta_survives_migration(migration_env))


def test_profile_fields_carry_over(migration_env):
    asyncio.run(_co_test_profile_fields_carry_over(migration_env))


def test_migration_is_idempotent(migration_env):
    asyncio.run(_co_test_migration_is_idempotent(migration_env))


def test_source_data_is_never_modified(migration_env):
    asyncio.run(_co_test_source_data_is_never_modified(migration_env))


def test_hippocampus_plugin_is_disabled_after_migration(migration_env):
    asyncio.run(_co_test_hippocampus_plugin_is_disabled_after_migration(migration_env))


def test_malformed_toml_does_not_abort_the_run(migration_env):
    asyncio.run(_co_test_malformed_toml_does_not_abort_the_run(migration_env))


def test_no_hippocampus_data_is_a_clean_noop(data_root):
    asyncio.run(_co_test_no_hippocampus_data_is_a_clean_noop(data_root))


async def _co_test_total_import_failure_does_not_disable_the_old_plugin(migration_env):
    """The most dangerous failure mode, pinned.

    If every memory fails to import but we still disable hippocampus and write
    the marker, the user loses the UI that reads their memories, the new plugin
    has nothing, and the marker stops the next launch from retrying — it reads
    as total data loss. Source files are untouched either way, so leaving both
    plugins enabled is the recoverable choice.
    """
    store = migration_env["store"]

    async def _boom(*_a, **_kw):
        raise RuntimeError("simulated store failure")

    store.add_memory = _boom  # type: ignore[method-assign]

    result, ctx = await _run(migration_env)

    assert result["migrated"] is False
    assert result.get("failed") is True
    assert ctx.plugin_mgr.calls == [], (
        "hippocampus was disabled despite importing nothing"
    )
    marker = migration_env["data_root"] / "memory" / ".hippocampus_migrated"
    assert not marker.exists(), "marker written on total failure; retry is now blocked"


def test_total_import_failure_does_not_disable_the_old_plugin(migration_env):
    asyncio.run(_co_test_total_import_failure_does_not_disable_the_old_plugin(migration_env))
