"""End-to-end tests for the hippocampus → KiraOS reconcile-based migration.

Migration is idempotent and incremental: re-running merges meta (earlier timestamp,
larger counts), picks up new hippocampus data, and never touches source. Archive
precedence: KiraOS archived memory → skip, never resurrect.

Source fixtures mirror hippocampus schema exactly:
- SQLite `memories` table with `id TEXT PRIMARY KEY` (no `storage_key`)
- entity dirs named `{type}_{quote(id)}` (paths.py:99), so colon-bearing ids
  like `telegram:12345` arrive percent-encoded as `telegram%3A12345`
- global/self/{facts,reflections}/ and global/skills/{skill_name}/
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sqlite3
import time
from pathlib import Path

import pytest

from plugins.KiraOS_Plugin.memory.memory_paths import set_data_root, get_archive_dir
from plugins.KiraOS_Plugin.memory.memory_index import MemoryIndex
from plugins.KiraOS_Plugin.memory.toml_tree_store import TomlTreeStore
from plugins.KiraOS_Plugin.memory.entity_profile import EntityProfileStore


# ── Source-tree builders (hippocampus format) ────────────────────────────────

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


def _write_toml(path, *, mem_id, mem_type, text, importance, tags, source=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f'id = "{mem_id}"',
        f'type = "{mem_type}"',
        f'text = "{text}"',
        f"importance = {importance}",
        f"tags = {json.dumps(tags)}",
    ]
    if source:
        lines.append(f"[source]")
        for k, v in source.items():
            if isinstance(v, str):
                lines.append(f'{k} = "{v}"')
            else:
                lines.append(f'{k} = {json.dumps(v)}')
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _seed_hippocampus(data_root, *, entity_dir_name, entity_id, entity_type):
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
    _write_toml(
        hippo / "global" / "self" / "facts" / "kira_self_fact.toml",
        mem_id="kira_self_fact",
        mem_type="fact",
        text="Kira 对自身行为有觉察",
        importance=8,
        tags=["self-awareness"],
    )
    _write_toml(
        hippo / "global" / "skills" / "code" / "python_skill.toml",
        mem_id="python_skill",
        mem_type="skill",
        text="熟悉 Python 编程",
        importance=9,
        tags=["coding"],
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

    # Aged meta: created ~30d ago, accessed often
    created = time.time() - 30 * 86400
    conn = sqlite3.connect(str(hippo / "memory_index.db"))
    conn.execute(_HIPPO_SCHEMA)
    conn.executemany(
        "INSERT INTO memories (id, entity_id, entity_type, folder, memory_type,"
        " importance, timestamp, last_accessed, access_count, file_path, raw_text)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [
            ("hates_css", entity_id, entity_type, "facts", "fact", 6,
             created, created + 86400, 17, str(ent / "facts" / "hates_css.toml"), "用户讨厌写 CSS"),
            ("prefers_backend", entity_id, entity_type, "reflections",
             "reflection", 7, created, created + 3600, 5, str(ent / "reflections" / "prefers_backend.toml"), "用户更偏好后端工作"),
            ("kira_likes_tea", "", "", "facts", "fact", 4,
             created, created, 2, str(hippo / "global" / "facts" / "kira_likes_tea.toml"), "Kira 喜欢喝茶"),
            ("kira_self_fact", "", "", "facts", "fact", 8,
             created, created, 1, str(hippo / "global" / "self" / "facts" / "kira_self_fact.toml"), "Kira 对自身行为有觉察"),
            ("python_skill", "", "", "code", "skill", 9,
             created, created, 3, str(hippo / "global" / "skills" / "code" / "python_skill.toml"), "熟悉 Python 编程"),
        ],
    )
    conn.commit()
    conn.close()
    return hippo


# ── Fixtures ─────────────────────────────────────────────────────────────────

_ENTITY_ID = "telegram:12345"
_ENTITY_TYPE = "user"
_ENTITY_DIR = "user_telegram%3A12345"


@pytest.fixture
def migration_env(data_root):
    """Seeded hippocampus tree + live KiraOS destination store."""
    hippo_root = _seed_hippocampus(
        data_root, entity_dir_name=_ENTITY_DIR, entity_id=_ENTITY_ID, entity_type=_ENTITY_TYPE
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
    from plugins.KiraOS_Plugin.memory.migration_hippocampus import migrate_hippocampus_if_needed
    ctx = _StubCtx()
    result = await migrate_hippocampus_if_needed(env["store"], env["profiles"], ctx)
    return result, ctx


# ── Tests ────────────────────────────────────────────────────────────────────


async def _co_test_migration_reports_all_stats(migration_env):
    """Seeded tree: 1 entity, 1 fact, 1 reflection, 1 global_facts, 1 global_self, 1 skills."""
    result, _ = await _run(migration_env)

    assert result["migrated"] is True
    assert result["entities"] == 1
    assert result["facts"] == 1
    assert result["reflections"] == 1
    assert result["global_facts"] == 1
    assert result["global_self"] == 1
    assert result["skills"] == 1


async def _co_test_migrated_memories_are_readable(migration_env):
    await _run(migration_env)
    store = migration_env["store"]

    facts = await store.get_all_memories(_ENTITY_ID, _ENTITY_TYPE, "facts")
    assert "用户讨厌写 CSS" in {m.text for m in facts}

    reflections = await store.get_all_memories(_ENTITY_ID, _ENTITY_TYPE, "reflections")
    assert "用户更偏好后端工作" in {m.text for m in reflections}

    # global/facts
    global_facts = await store.get_all_memories("", "", "facts", "global")
    assert "Kira 喜欢喝茶" in {m.text for m in global_facts}

    # global/self
    self_facts = await store.get_all_memories("", "", "facts", "global/self")
    assert "Kira 对自身行为有觉察" in {m.text for m in self_facts}

    # global/skills
    skills = await store.get_all_memories("", "", "code", "global/skills")
    assert "熟悉 Python 编程" in {m.text for m in skills}


async def _co_test_aged_meta_survives(migration_env):
    await _run(migration_env)

    facts = await migration_env["store"].get_all_memories(_ENTITY_ID, _ENTITY_TYPE, "facts")
    mem = next((m for m in facts if m.id == "hates_css"), None)
    assert mem is not None

    age_days = (time.time() - mem.timestamp) / 86400
    assert age_days > 25
    assert mem.access_count == 17


async def _co_test_profile_carries_over(migration_env):
    await _run(migration_env)
    profile = await migration_env["profiles"].get_profile(_ENTITY_ID, _ENTITY_TYPE)
    assert profile.name == "阿柚"
    assert profile.interaction_count == 42
    assert "耐心" in profile.traits


async def _co_test_reconcile_idempotent(migration_env):
    """Marker is JSON manifest, re-run skips processed files."""
    first, _ = await _run(migration_env)
    assert first["migrated"] is True

    second, _ = await _run(migration_env)
    assert second["migrated"] is True
    assert second["facts"] == 0  # already processed
    assert second["reflections"] == 0

    facts = await migration_env["store"].get_all_memories(_ENTITY_ID, _ENTITY_TYPE, "facts")
    assert len([m for m in facts if m.id == "hates_css"]) == 1


async def _co_test_incremental_new_data(migration_env):
    """Add new hippocampus file after first run → second run picks it up."""
    first, _ = await _run(migration_env)
    assert first["facts"] == 1

    # Add new hippocampus fact
    hippo_ent = migration_env["hippo_root"] / "entities" / _ENTITY_DIR / "facts"
    _write_toml(
        hippo_ent / "new_fact.toml",
        mem_id="new_fact",
        mem_type="fact",
        text="新增事实",
        importance=5,
        tags=["test"],
    )

    second, _ = await _run(migration_env)
    assert second["migrated"] is True
    assert second["facts"] == 1  # picked up the new one

    facts = await migration_env["store"].get_all_memories(_ENTITY_ID, _ENTITY_TYPE, "facts")
    assert "新增事实" in {m.text for m in facts}


async def _co_test_archive_precedence(migration_env):
    """KiraOS archived memory → reconcile skips it."""
    store = migration_env["store"]

    # First import
    await _run(migration_env)
    facts_before = await store.get_all_memories(_ENTITY_ID, _ENTITY_TYPE, "facts")
    assert "用户讨厌写 CSS" in {m.text for m in facts_before}

    # Archive it in KiraOS
    ok = await store.archive_memory("hates_css", _ENTITY_ID, _ENTITY_TYPE, "facts")
    assert ok is True
    facts_after = await store.get_all_memories(_ENTITY_ID, _ENTITY_TYPE, "facts")
    assert "用户讨厌写 CSS" not in {m.text for m in facts_after}

    # Reset manifest to trigger re-reconcile
    marker_path = migration_env["data_root"] / "memory" / ".hippocampus_migrated"
    marker_path.unlink()

    # Re-run: archived memory should NOT be resurrected
    second, _ = await _run(migration_env)
    assert second["migrated"] is True

    facts_final = await store.get_all_memories(_ENTITY_ID, _ENTITY_TYPE, "facts")
    assert "用户讨厌写 CSS" not in {m.text for m in facts_final}


async def _co_test_merge_meta_earlier_timestamp(migration_env):
    """KiraOS has it with later timestamp → reconcile updates to earlier."""
    store = migration_env["store"]

    # Pre-seed KiraOS with newer timestamp
    newer_ts = time.time()
    await store.add_memory(
        content_text="用户讨厌写 CSS",
        memory_type="fact",
        importance=6,
        tags=["frontend", "preference"],
        semantic_id="hates_css",
        entity_id=_ENTITY_ID,
        entity_type=_ENTITY_TYPE,
        folder="facts",
    )
    mem_before = await store.get_memory("hates_css", _ENTITY_ID, _ENTITY_TYPE, "facts")
    assert mem_before.timestamp >= newer_ts

    # Reconcile: hippocampus has older timestamp
    await _run(migration_env)

    mem_after = await store.get_memory("hates_css", _ENTITY_ID, _ENTITY_TYPE, "facts")
    assert mem_after.timestamp < newer_ts  # took hippocampus's older timestamp


async def _co_test_source_untouched(migration_env):
    hippo = migration_env["hippo_root"]
    before = {
        p: p.read_bytes()
        for p in hippo.rglob("*")
        if p.is_file() and p.suffix in {".toml", ".json"}
    }
    assert before

    await _run(migration_env)

    for path, content in before.items():
        assert path.exists()
        assert path.read_bytes() == content


async def _co_test_hippocampus_plugin_disabled(migration_env):
    _, ctx = await _run(migration_env)
    assert ("kira_plugin_hippocampus_memory", False) in ctx.plugin_mgr.calls


async def _co_test_malformed_toml_nonfatal(migration_env):
    bad = (
        migration_env["hippo_root"] / "entities" / _ENTITY_DIR / "facts" / "broken.toml"
    )
    bad.write_text('id = "broken"\ntext = "unterminated', encoding="utf-8")

    result, _ = await _run(migration_env)
    assert result["migrated"] is True
    assert result["facts"] >= 1

    facts = await migration_env["store"].get_all_memories(_ENTITY_ID, _ENTITY_TYPE, "facts")
    assert "用户讨厌写 CSS" in {m.text for m in facts}


async def _co_test_no_hippocampus_noop(data_root):
    set_data_root(data_root / "memory")
    index = MemoryIndex()
    try:
        store = TomlTreeStore(index=index)
        from plugins.KiraOS_Plugin.memory.migration_hippocampus import migrate_hippocampus_if_needed

        result = await migrate_hippocampus_if_needed(store, EntityProfileStore(), _StubCtx())
        assert result["migrated"] is False
        marker_path = data_root / "memory" / ".hippocampus_migrated"
        assert marker_path.exists()
        content = marker_path.read_text(encoding="utf-8")
        manifest = json.loads(content)
        assert manifest["version"] == 2
    finally:
        index.close()


async def _co_test_total_import_failure_no_disable(migration_env):
    store = migration_env["store"]

    async def _boom(*_a, **_kw):
        raise RuntimeError("simulated store failure")

    store.add_memory = _boom  # type: ignore[method-assign]

    result, ctx = await _run(migration_env)
    assert result["migrated"] is False
    assert result.get("failed") is True
    assert ctx.plugin_mgr.calls == []

    marker = migration_env["data_root"] / "memory" / ".hippocampus_migrated"
    assert not marker.exists()


async def _co_test_v1_marker_upgraded(migration_env):
    """Old plain .hippocampus_migrated file → reconcile still runs, upgrades to v2 manifest."""
    marker_path = migration_env["data_root"] / "memory" / ".hippocampus_migrated"
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    marker_path.write_text('{"entities": 1}', encoding="utf-8")  # old v1 format

    result, _ = await _run(migration_env)
    assert result["migrated"] is True
    assert result["entities"] == 1

    content = marker_path.read_text(encoding="utf-8")
    manifest = json.loads(content)
    assert manifest["version"] == 2
    assert "processed" in manifest


# ── Upgrade paths & reconcile semantics ──────────────────────────────────────


def _marker(env):
    return env["data_root"] / "memory" / ".hippocampus_migrated"


def _manifest(env):
    return json.loads(_marker(env).read_text(encoding="utf-8"))


_HATES_CSS_REL = f"entities/{_ENTITY_DIR}/facts/hates_css.toml"


async def _co_test_rerun_not_reported_as_failure(migration_env):
    """4.0.x reported a clean re-run (0 new imports) as a failure."""
    await _run(migration_env)
    second, ctx = await _run(migration_env)

    assert second["migrated"] is True
    assert "failed" not in second
    assert second["errors"] == 0
    assert second["up_to_date"] == 5
    assert ctx.plugin_mgr.calls == []  # disabled once, not on every launch


async def _co_test_v40x_stats_marker_partial_import_repaired(migration_env):
    """4.0.x imported only entity memories + global/facts, with fresh meta.

    Upgrade must restore the lost meta in place (no duplicates), add what 4.0.x
    dropped (global/self, skills), and not re-disable a plugin the user may
    have re-enabled since.
    """
    store = migration_env["store"]
    for mem_id, text, kw in (
        ("hates_css", "用户讨厌写 CSS",
         dict(entity_id=_ENTITY_ID, entity_type=_ENTITY_TYPE, folder="facts")),
        ("prefers_backend", "用户更偏好后端工作",
         dict(entity_id=_ENTITY_ID, entity_type=_ENTITY_TYPE, folder="reflections")),
        ("kira_likes_tea", "Kira 喜欢喝茶", dict(folder="facts", base_dir="global")),
    ):
        await store.add_memory(content_text=text, semantic_id=mem_id, **kw)
    marker = _marker(migration_env)
    marker.write_text('{"entities": 1, "facts": 1, "reflections": 1}', encoding="utf-8")

    result, ctx = await _run(migration_env)

    assert result["migrated"] is True
    assert (result["facts"], result["reflections"], result["global_facts"]) == (0, 0, 0)
    assert result["merged"] == 3
    assert result["global_self"] == 1
    assert result["skills"] == 1
    assert ctx.plugin_mgr.calls == []

    facts = await store.get_all_memories(_ENTITY_ID, _ENTITY_TYPE, "facts")
    assert [m.id for m in facts] == ["hates_css"]
    assert facts[0].access_count == 17
    assert (time.time() - facts[0].timestamp) / 86400 > 25

    manifest = _manifest(migration_env)
    assert manifest["version"] == 2
    assert manifest["plugin_disabled"] is True
    assert manifest["since"] is not None
    assert manifest["processed"][_HATES_CSS_REL]["state"] == "merged"


async def _co_test_v40x_empty_marker_imports_later_data(migration_env):
    """4.0.x touched an empty marker when no hippocampus data existed yet and
    then never looked again. Data that appears later must still be imported."""
    _marker(migration_env).write_text("", encoding="utf-8")

    result, ctx = await _run(migration_env)

    assert result["migrated"] is True
    assert result["facts"] == 1
    assert result["global_self"] == 1
    assert ("kira_plugin_hippocampus_memory", False) in ctx.plugin_mgr.calls


async def _co_test_slug_collision_kept_and_not_duplicated(migration_env):
    """A KiraOS-native memory already owns the slug with a different text.

    Both survive; re-running without a manifest (4.0.x marker) finds the
    `{id}_{hash8}` copy instead of adding a third one.
    """
    store = migration_env["store"]
    await store.add_memory(
        content_text="用户在学 Rust", semantic_id="hates_css",
        entity_id=_ENTITY_ID, entity_type=_ENTITY_TYPE, folder="facts",
    )

    first, _ = await _run(migration_env)
    assert first["facts"] == 1

    facts = {m.id: m.text for m in
             await store.get_all_memories(_ENTITY_ID, _ENTITY_TYPE, "facts")}
    assert facts["hates_css"] == "用户在学 Rust"  # native memory untouched
    suffixed = [i for i in facts if i.startswith("hates_css_")]
    assert len(suffixed) == 1 and facts[suffixed[0]] == "用户讨厌写 CSS"

    _marker(migration_env).write_text('{"facts": 1}', encoding="utf-8")
    second, _ = await _run(migration_env)
    assert second["facts"] == 0

    after = await store.get_all_memories(_ENTITY_ID, _ENTITY_TYPE, "facts")
    assert sorted(m.id for m in after) == sorted(facts)


async def _co_test_same_text_across_entities_both_imported(migration_env):
    """4.0.x deduped by content hash globally and dropped the second entity's
    copy. Namespaced matching keeps both, and the shared hippocampus index row
    (global PK `id`) does not leak entity A's meta onto entity B."""
    hippo = migration_env["hippo_root"]
    other_dir = "user_telegram%3A999"
    _write_toml(
        hippo / "entities" / other_dir / "facts" / "hates_css.toml",
        mem_id="hates_css", mem_type="fact", text="用户讨厌写 CSS",
        importance=6, tags=["frontend"],
    )

    result, _ = await _run(migration_env)
    assert result["facts"] == 2

    store = migration_env["store"]
    a = await store.get_memory("hates_css", _ENTITY_ID, _ENTITY_TYPE, "facts")
    b = await store.get_memory("hates_css", "telegram:999", _ENTITY_TYPE, "facts")
    assert a is not None and b is not None
    assert a.access_count == 17
    assert b.access_count == 0


async def _co_test_deleted_kiraos_memory_not_resurrected(migration_env):
    """Tombstone: a previously reconciled memory was deleted from KiraOS, so
    the next run skips the source file even if it has changed."""
    await _run(migration_env)
    store = migration_env["store"]
    ok = await store.delete_memory("hates_css", _ENTITY_ID, _ENTITY_TYPE, "facts")
    assert ok is True

    hippo = migration_env["hippo_root"]
    src = hippo / "entities" / _ENTITY_DIR / "facts" / "hates_css.toml"
    src.write_text('id="hates_css"\ntext="新文本"\nimportance=8\ntags=[]', encoding="utf-8")

    second, _ = await _run(migration_env)
    assert second["migrated"] is True
    assert second["facts"] == 0

    after = await store.get_memory("hates_css", _ENTITY_ID, _ENTITY_TYPE, "facts")
    assert after is None
    assert _manifest(migration_env)["processed"][_HATES_CSS_REL]["state"] == "deleted"


async def _co_test_archived_kiraos_memory_not_resurrected(migration_env):
    """Like deleted, but KiraOS archived it (decay or user action)."""
    await _run(migration_env)
    store = migration_env["store"]
    ok = await store.archive_memory("hates_css", _ENTITY_ID, _ENTITY_TYPE, "facts")
    assert ok is True

    hippo = migration_env["hippo_root"]
    (hippo / "entities" / _ENTITY_DIR / "facts" / "hates_css.toml").write_text(
        'id="hates_css"\ntext="新文本"\nimportance=8\ntags=[]', encoding="utf-8",
    )

    _marker(migration_env).unlink()
    second, _ = await _run(migration_env)

    assert second["migrated"] is True
    assert second["facts"] == 0
    assert await store.get_memory("hates_css", _ENTITY_ID, _ENTITY_TYPE, "facts") is None
    assert _manifest(migration_env)["processed"][_HATES_CSS_REL]["state"] == "archived"


async def _co_test_entity_deleted_from_webui_not_reimported(migration_env):
    """WebUI's `api_delete_entity` moved the entity dir to
    `archive/_full_entities/{type}_{id}_{unix_ts}` after the first import.
    Neither changed nor newly appeared hippocampus files bring it back."""
    first, _ = await _run(migration_env)
    assert first["facts"] == 1

    memory_root = migration_env["data_root"] / "memory"
    archive = memory_root / "archive" / "_full_entities"
    archive.mkdir(parents=True, exist_ok=True)
    shutil.move(
        str(memory_root / "entities" / _ENTITY_DIR),
        str(archive / f"{_ENTITY_DIR}_{int(time.time())}"),
    )

    hippo_facts = migration_env["hippo_root"] / "entities" / _ENTITY_DIR / "facts"
    (hippo_facts / "hates_css.toml").write_text(
        'id="hates_css"\ntext="新文本"\nimportance=8\ntags=[]', encoding="utf-8",
    )
    _write_toml(hippo_facts / "late_fact.toml", mem_id="late_fact", mem_type="fact",
                text="删除后才出现的事实", importance=5, tags=[])

    second, _ = await _run(migration_env)

    assert second["facts"] == 0
    assert second["entities"] == 0
    assert not (memory_root / "entities" / _ENTITY_DIR / "facts").exists()
    processed = _manifest(migration_env)["processed"]
    assert processed[_HATES_CSS_REL]["state"] == "entity_deleted"
    assert processed[f"entities/{_ENTITY_DIR}/facts/late_fact.toml"]["state"] == "entity_deleted"

    # The tombstone outlives the evidence: purging the KiraOS archive and
    # editing the source again still does not bring the entity back.
    shutil.rmtree(archive)
    (hippo_facts / "hates_css.toml").write_text(
        'id="hates_css"\ntext="再改一次"\nimportance=8\ntags=[]', encoding="utf-8",
    )
    third, _ = await _run(migration_env)
    assert third["facts"] == 0
    assert not (memory_root / "entities" / _ENTITY_DIR / "facts").exists()


async def _co_test_update_from_unchanged_kiraos_import(migration_env):
    """Hippocampus edited an already-imported memory. KiraOS's copy is untouched
    since the import (text_hash = record hash), so the hippocampus edit wins."""
    await _run(migration_env)
    store = migration_env["store"]
    mem = await store.get_memory("hates_css", _ENTITY_ID, _ENTITY_TYPE, "facts")
    assert mem.text == "用户讨厌写 CSS"
    assert mem.access_count == 17

    hippo = migration_env["hippo_root"]
    (hippo / "entities" / _ENTITY_DIR / "facts" / "hates_css.toml").write_text(
        'id="hates_css"\ntext="用户超级讨厌写 CSS"\nimportance=9\ntags=["frontend"]',
        encoding="utf-8",
    )

    second, _ = await _run(migration_env)
    assert second["merged"] == 1
    after = await store.get_memory("hates_css", _ENTITY_ID, _ENTITY_TYPE, "facts")
    assert after.text == "用户超级讨厌写 CSS"
    assert after.importance == 9
    assert after.access_count == 17


async def _co_test_kiraos_edited_import_keeps_kiraos_text(migration_env):
    """Both hippocampus and KiraOS edited the text after the import. KiraOS's
    edit wins; only meta is merged."""
    await _run(migration_env)
    store = migration_env["store"]
    mem = await store.get_memory("hates_css", _ENTITY_ID, _ENTITY_TYPE, "facts")
    mem.text = "用户其实还是写了 CSS"
    ok = await store.update_memory(mem)
    assert ok is True

    hippo = migration_env["hippo_root"]
    src = hippo / "entities" / _ENTITY_DIR / "facts" / "hates_css.toml"
    src.write_text(
        'id="hates_css"\ntext="用户讨厌写 CSS 但拿它没办法"\nimportance=8\ntags=["frontend"]',
        encoding="utf-8",
    )

    second, _ = await _run(migration_env)
    assert second["errors"] == 0
    after = await store.get_memory("hates_css", _ENTITY_ID, _ENTITY_TYPE, "facts")
    assert after.text == "用户其实还是写了 CSS"
    assert after.access_count == 17
    facts = await store.get_all_memories(_ENTITY_ID, _ENTITY_TYPE, "facts")
    assert len(facts) == 1


async def _co_test_incremental_archive_copy_no_overwrite(migration_env):
    """Hippocampus archive/ is copied to KiraOS archive/hippocampus/; later
    files are added incrementally, existing ones never overwritten."""
    hippo = migration_env["hippo_root"]
    (hippo / "archive").mkdir()
    (hippo / "archive" / "old.toml").write_text("old content", encoding="utf-8")
    await _run(migration_env)

    kiraos_archive = migration_env["data_root"] / "memory" / "archive" / "hippocampus"
    assert (kiraos_archive / "old.toml").read_text(encoding="utf-8") == "old content"

    (kiraos_archive / "old.toml").write_text("KiraOS changed it", encoding="utf-8")
    (hippo / "archive" / "new.toml").write_text("new content", encoding="utf-8")

    second, _ = await _run(migration_env)
    assert (kiraos_archive / "old.toml").read_text(encoding="utf-8") == "KiraOS changed it"
    assert (kiraos_archive / "new.toml").read_text(encoding="utf-8") == "new content"


async def _co_test_profile_remerged_on_change(migration_env):
    """The first import recorded the profile's hash. When the hippocampus
    profile changes, the next run merges again (union behavior)."""
    await _run(migration_env)
    profiles = migration_env["profiles"]
    before = await profiles.get_profile(_ENTITY_ID, _ENTITY_TYPE)
    assert before.traits == ["耐心", "技术导向"]

    hippo = migration_env["hippo_root"]
    profile_json = hippo / "entities" / _ENTITY_DIR / "profile.json"
    data = json.loads(profile_json.read_text(encoding="utf-8"))
    data["traits"].append("高效")
    profile_json.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    second, _ = await _run(migration_env)
    assert second["entities"] == 1
    after = await profiles.get_profile(_ENTITY_ID, _ENTITY_TYPE)
    assert "高效" in after.traits


async def _co_test_legacy_json_memory_imported(migration_env):
    """Pre-TOML hippocampus format: {id, type, content: {raw_text}, meta: {}}."""
    hippo = migration_env["hippo_root"]
    json_mem = hippo / "entities" / _ENTITY_DIR / "facts" / "loves_python.json"
    json_mem.write_text(json.dumps({
        "id": "loves_python",
        "type": "fact",
        "content": {"raw_text": "用户热爱 Python"},
        "meta": {"importance": 7, "tags": ["coding"]},
    }, ensure_ascii=False), encoding="utf-8")

    result, _ = await _run(migration_env)
    assert result["facts"] == 2

    mem = await migration_env["store"].get_memory(
        "loves_python", _ENTITY_ID, _ENTITY_TYPE, "facts",
    )
    assert mem is not None
    assert mem.text == "用户热爱 Python"
    assert mem.importance == 7


async def _co_test_entity_skills_folder_imported(migration_env):
    """Entity skills live in `entities/{dir}/skills/{skill_name}.toml`.

    The ENTITY_FOLDERS constant includes `"skills"`, and they are bucketed
    separately in stats (not lumped into facts)."""
    hippo = migration_env["hippo_root"]
    _write_toml(
        hippo / "entities" / _ENTITY_DIR / "skills" / "docker_skill.toml",
        mem_id="docker_skill", mem_type="skill", text="会用 Docker", importance=8, tags=["devops"],
    )

    result, _ = await _run(migration_env)
    assert result["skills"] == 2

    mem = await migration_env["store"].get_memory(
        "docker_skill", _ENTITY_ID, _ENTITY_TYPE, "skills",
    )
    assert mem is not None
    assert mem.text == "会用 Docker"


async def _co_test_relocated_data_root_meta_restored(migration_env):
    """SQLite `file_path` is absolute. A moved data root still matches (path is
    compared relative to the memory root); a row without a usable `file_path`
    falls back to id + content hash."""
    hippo = migration_env["hippo_root"]
    conn = sqlite3.connect(str(hippo / "memory_index.db"))
    conn.execute(
        "UPDATE memories SET file_path = '/old/install/memory'"
        " || substr(file_path, instr(file_path, '/entities/')) WHERE id = 'hates_css'"
    )
    conn.execute("UPDATE memories SET file_path = '' WHERE id = 'prefers_backend'")
    conn.commit()
    conn.close()

    result, _ = await _run(migration_env)
    assert result["facts"] == 1
    assert result["reflections"] == 1

    store = migration_env["store"]
    fact = await store.get_memory("hates_css", _ENTITY_ID, _ENTITY_TYPE, "facts")
    assert fact.access_count == 17
    assert (time.time() - fact.timestamp) / 86400 > 25
    refl = await store.get_memory("prefers_backend", _ENTITY_ID, _ENTITY_TYPE, "reflections")
    assert refl.access_count == 5
    assert (time.time() - refl.timestamp) / 86400 > 25


# ── PR #24 review regressions ────────────────────────────────────────────────


async def _co_test_kiraos_edit_survives_repeated_source_edits(migration_env):
    """A KiraOS edit must keep winning, not only on the first source edit."""
    await _run(migration_env)
    store = migration_env["store"]
    mem = await store.get_memory("hates_css", _ENTITY_ID, _ENTITY_TYPE, "facts")
    mem.text = "用户其实还是写了 CSS"
    assert await store.update_memory(mem)

    src = migration_env["hippo_root"] / "entities" / _ENTITY_DIR / "facts" / "hates_css.toml"
    for text in ("第一次改", "第二次改", "第三次改"):
        src.write_text(f'id="hates_css"\ntext="{text}"\nimportance=8\ntags=[]',
                       encoding="utf-8")
        result, _ = await _run(migration_env)
        assert result["errors"] == 0
        after = await store.get_memory("hates_css", _ENTITY_ID, _ENTITY_TYPE, "facts")
        assert after.text == "用户其实还是写了 CSS", text


async def _co_test_all_sources_unreadable_no_disable(migration_env):
    """Every source file unreadable: nothing landed, so hippocampus stays
    enabled and no manifest is written (retried next launch)."""
    hippo = migration_env["hippo_root"]
    for path in hippo.rglob("*.toml"):
        path.write_text('id = "x"\ntext = "unterminated', encoding="utf-8")
    (hippo / "entities" / _ENTITY_DIR / "profile.json").write_text("{nope", encoding="utf-8")

    result, ctx = await _run(migration_env)
    assert result["migrated"] is False
    assert result.get("failed") is True
    assert ctx.plugin_mgr.calls == []
    assert not _marker(migration_env).exists()


async def _co_test_entity_tombstone_is_entity_wide_and_persisted(migration_env):
    """Unchanged files of a deleted entity are tombstoned too, and the entity
    itself is remembered: once the archive dir is gone, a new source file or a
    changed profile still does not recreate it."""
    await _run(migration_env)
    memory_root = migration_env["data_root"] / "memory"
    archive = memory_root / "archive" / "_full_entities"
    archive.mkdir(parents=True, exist_ok=True)
    shutil.move(
        str(memory_root / "entities" / _ENTITY_DIR),
        str(archive / f"{_ENTITY_DIR}_{int(time.time())}"),
    )

    await _run(migration_env)  # source unchanged
    manifest = _manifest(migration_env)
    assert manifest["processed"][_HATES_CSS_REL]["state"] == "entity_deleted"
    assert manifest["profiles"][f"entities/{_ENTITY_DIR}/profile.json"]["state"] == "entity_deleted"
    assert [_ENTITY_TYPE, _ENTITY_ID] in manifest["deleted_entities"]

    shutil.rmtree(archive)
    hippo_ent = migration_env["hippo_root"] / "entities" / _ENTITY_DIR
    _write_toml(hippo_ent / "facts" / "late_fact.toml", mem_id="late_fact", mem_type="fact",
                text="归档清空后才出现的事实", importance=5, tags=[])
    profile = json.loads((hippo_ent / "profile.json").read_text(encoding="utf-8"))
    profile["facts"].append("新画像事实")
    (hippo_ent / "profile.json").write_text(json.dumps(profile, ensure_ascii=False),
                                            encoding="utf-8")

    third, _ = await _run(migration_env)
    assert third["facts"] == 0 and third["entities"] == 0
    assert not (memory_root / "entities" / _ENTITY_DIR).exists()


async def _co_test_unloadable_kiraos_profile_not_overwritten(migration_env):
    """A KiraOS profile that is valid JSON but not loadable (no entity_id)
    would come back from get_profile as an empty default; it must not be
    saved over."""
    target = migration_env["data_root"] / "memory" / "entities" / _ENTITY_DIR / "profile.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    original = json.dumps({"name": "手写", "facts": ["保留我"]}, ensure_ascii=False)
    target.write_text(original, encoding="utf-8")

    result, _ = await _run(migration_env)
    assert result["errors"] >= 1
    assert result["entities"] == 0
    assert target.read_text(encoding="utf-8") == original


async def _co_test_damaged_manifest_kept(migration_env, content):
    """A damaged or unknown manifest raises (main.py logs it, non-fatal) and is
    left as it is, instead of being replaced by an empty one that has lost
    every tombstone."""
    from plugins.KiraOS_Plugin.memory.migration_hippocampus import migrate_hippocampus_if_needed

    await _run(migration_env)
    _marker(migration_env).write_text(content, encoding="utf-8")
    with pytest.raises(RuntimeError):
        await migrate_hippocampus_if_needed(
            migration_env["store"], migration_env["profiles"], _StubCtx()
        )
    assert _marker(migration_env).read_text(encoding="utf-8") == content


# === Sync wrappers ===

def test_migration_reports_all_stats(migration_env):
    asyncio.run(_co_test_migration_reports_all_stats(migration_env))


def test_migrated_memories_are_readable(migration_env):
    asyncio.run(_co_test_migrated_memories_are_readable(migration_env))


def test_aged_meta_survives(migration_env):
    asyncio.run(_co_test_aged_meta_survives(migration_env))


def test_profile_carries_over(migration_env):
    asyncio.run(_co_test_profile_carries_over(migration_env))


def test_reconcile_idempotent(migration_env):
    asyncio.run(_co_test_reconcile_idempotent(migration_env))


def test_incremental_new_data(migration_env):
    asyncio.run(_co_test_incremental_new_data(migration_env))


def test_archive_precedence(migration_env):
    asyncio.run(_co_test_archive_precedence(migration_env))


def test_merge_meta_earlier_timestamp(migration_env):
    asyncio.run(_co_test_merge_meta_earlier_timestamp(migration_env))


def test_source_untouched(migration_env):
    asyncio.run(_co_test_source_untouched(migration_env))


def test_hippocampus_plugin_disabled(migration_env):
    asyncio.run(_co_test_hippocampus_plugin_disabled(migration_env))


def test_malformed_toml_nonfatal(migration_env):
    asyncio.run(_co_test_malformed_toml_nonfatal(migration_env))


def test_no_hippocampus_noop(data_root):
    asyncio.run(_co_test_no_hippocampus_noop(data_root))


def test_total_import_failure_no_disable(migration_env):
    asyncio.run(_co_test_total_import_failure_no_disable(migration_env))


def test_v1_marker_upgraded(migration_env):
    asyncio.run(_co_test_v1_marker_upgraded(migration_env))


def test_rerun_not_reported_as_failure(migration_env):
    asyncio.run(_co_test_rerun_not_reported_as_failure(migration_env))


def test_v40x_stats_marker_partial_import_repaired(migration_env):
    asyncio.run(_co_test_v40x_stats_marker_partial_import_repaired(migration_env))


def test_v40x_empty_marker_imports_later_data(migration_env):
    asyncio.run(_co_test_v40x_empty_marker_imports_later_data(migration_env))


def test_slug_collision_kept_and_not_duplicated(migration_env):
    asyncio.run(_co_test_slug_collision_kept_and_not_duplicated(migration_env))


def test_same_text_across_entities_both_imported(migration_env):
    asyncio.run(_co_test_same_text_across_entities_both_imported(migration_env))


def test_deleted_kiraos_memory_not_resurrected(migration_env):
    asyncio.run(_co_test_deleted_kiraos_memory_not_resurrected(migration_env))


def test_archived_kiraos_memory_not_resurrected(migration_env):
    asyncio.run(_co_test_archived_kiraos_memory_not_resurrected(migration_env))


def test_entity_deleted_from_webui_not_reimported(migration_env):
    asyncio.run(_co_test_entity_deleted_from_webui_not_reimported(migration_env))


def test_update_from_unchanged_kiraos_import(migration_env):
    asyncio.run(_co_test_update_from_unchanged_kiraos_import(migration_env))


def test_kiraos_edited_import_keeps_kiraos_text(migration_env):
    asyncio.run(_co_test_kiraos_edited_import_keeps_kiraos_text(migration_env))


def test_incremental_archive_copy_no_overwrite(migration_env):
    asyncio.run(_co_test_incremental_archive_copy_no_overwrite(migration_env))


def test_profile_remerged_on_change(migration_env):
    asyncio.run(_co_test_profile_remerged_on_change(migration_env))


def test_legacy_json_memory_imported(migration_env):
    asyncio.run(_co_test_legacy_json_memory_imported(migration_env))


def test_entity_skills_folder_imported(migration_env):
    asyncio.run(_co_test_entity_skills_folder_imported(migration_env))


def test_relocated_data_root_meta_restored(migration_env):
    asyncio.run(_co_test_relocated_data_root_meta_restored(migration_env))


def test_kiraos_edit_survives_repeated_source_edits(migration_env):
    asyncio.run(_co_test_kiraos_edit_survives_repeated_source_edits(migration_env))


def test_all_sources_unreadable_no_disable(migration_env):
    asyncio.run(_co_test_all_sources_unreadable_no_disable(migration_env))


def test_entity_tombstone_is_entity_wide_and_persisted(migration_env):
    asyncio.run(_co_test_entity_tombstone_is_entity_wide_and_persisted(migration_env))


def test_unloadable_kiraos_profile_not_overwritten(migration_env):
    asyncio.run(_co_test_unloadable_kiraos_profile_not_overwritten(migration_env))


@pytest.mark.parametrize("content", ["{broken", '{"version": 99, "processed": {}}', "[1, 2]"])
def test_damaged_manifest_kept(migration_env, content):
    asyncio.run(_co_test_damaged_manifest_kept(migration_env, content))
