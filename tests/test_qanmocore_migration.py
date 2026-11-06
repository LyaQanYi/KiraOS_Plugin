"""MaiBot -> KiraOS importer tests.

A tiny synthetic MaiBot database exercises every source mapping, the
ownership/idempotency rules, and the read-only guarantee. The fixtures mirror
the real schema columns the importer reads from the source database.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from unittest.mock import Mock
from urllib.parse import quote

import pytest

try:
    import tomllib
except ImportError:  # Python 3.10
    import tomli as tomllib

from plugins.KiraOS_Plugin.memory.entity_profile import EntityProfileStore
from plugins.KiraOS_Plugin.memory import (
    entity_profile, memory_index, memory_paths, migration_qanmocore, toml_tree_store,
)
from plugins.KiraOS_Plugin.memory.memory_index import MemoryIndex
from plugins.KiraOS_Plugin.memory.memory_paths import (
    ensure_directory_structure,
    set_data_root,
)
from plugins.KiraOS_Plugin.memory.migration_qanmocore import (
    DEFAULT_INCLUDE,
    ImportOptions,
    import_qanmocore,
)
from plugins.KiraOS_Plugin.scripts import import_qanmocore as cli
from plugins.KiraOS_Plugin.memory.toml_tree_store import TomlTreeStore


SCHEMA = {
    "chat_streams": (
        "CREATE TABLE chat_streams (stream_id TEXT, platform TEXT, group_id TEXT, "
        "group_name TEXT, user_id TEXT, user_nickname TEXT, last_active_time REAL)"
    ),
    "person_info": (
        "CREATE TABLE person_info (id INTEGER PRIMARY KEY, is_known INTEGER, "
        "person_id TEXT, person_name TEXT, name_reason TEXT, platform TEXT, "
        "user_id TEXT, nickname TEXT, memory_points TEXT, know_times REAL, "
        "know_since REAL, last_know REAL, group_nick_name TEXT)"
    ),
    "chat_history": (
        "CREATE TABLE chat_history (id INTEGER PRIMARY KEY, chat_id TEXT, "
        "start_time REAL, end_time REAL, original_text TEXT, participants TEXT, "
        "theme TEXT, keywords TEXT, summary TEXT, count INTEGER, "
        "forget_times INTEGER, key_point TEXT)"
    ),
    "memory_chest": (
        "CREATE TABLE memory_chest (id INTEGER PRIMARY KEY, title TEXT, "
        "content TEXT, chat_id TEXT, locked INTEGER)"
    ),
    "jargon": (
        "CREATE TABLE jargon (id INTEGER PRIMARY KEY, content TEXT, raw_content TEXT, "
        "meaning TEXT, chat_id TEXT, is_global INTEGER, count INTEGER, "
        "is_jargon INTEGER, last_inference_count INTEGER, is_complete INTEGER, "
        "inference_with_context TEXT, inference_content_only TEXT)"
    ),
    "thinking_back": (
        "CREATE TABLE thinking_back (id INTEGER PRIMARY KEY, chat_id TEXT, "
        "question TEXT, context TEXT, found_answer INTEGER, answer TEXT, "
        "thinking_steps TEXT, create_time REAL, update_time REAL)"
    ),
    "memory_conflicts": (
        "CREATE TABLE memory_conflicts (id INTEGER PRIMARY KEY, conflict_content TEXT, "
        "answer TEXT, create_time REAL, update_time REAL, context TEXT, "
        "chat_id TEXT, raise_time REAL)"
    ),
    "memory": (
        "CREATE TABLE memory (id INTEGER PRIMARY KEY, memory_id TEXT, chat_id TEXT, "
        "memory_text TEXT, keywords TEXT, create_time REAL, last_view_time REAL)"
    ),
}


def _make_source(tmp_path: Path) -> Path:
    source = tmp_path / "qanmocore"
    source.mkdir()
    db = source / "MaiBot.db"
    conn = sqlite3.connect(db)
    try:
        for sql in SCHEMA.values():
            conn.execute(sql)
        conn.executemany(
            "INSERT INTO chat_streams VALUES (?,?,?,?,?,?,?)",
            [
                ("s_group1", "qq", "100", "测试一群", "900", "群主", 2000.0),
                ("s_group2", "qq", "200", "测试二群", "901", "群主2", 2100.0),
                ("s_dm", "qq", "", "", "777", "私聊用户", 1500.0),
            ],
        )
        conn.execute(
            "INSERT INTO person_info VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                1, 1, "p1", "小明", "注册", "qq", "123", "小明",
                json.dumps(
                    ["身份:小明是学生:3.0", "性格:小明喜欢猫:5.0", "坏数据"],
                    ensure_ascii=False,
                ),
                2.0, 1000.0, 1100.0,
                json.dumps(
                    [{"group_id": "100", "group_nick_name": "小明明"}],
                    ensure_ascii=False,
                ),
            ),
        )
        conn.execute(
            "INSERT INTO person_info VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (2, 1, "p2", "小红", "注册", "qq", "124", "小红", "[]",
             1.0, None, None, "[]"),
        )
        conn.executemany(
            "INSERT INTO chat_history VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (1, "s_group1", 1000, 1200, "原始文本", json.dumps(["小明"]),
                 "主题A", json.dumps(["显卡"]), "摘要A", 0, 0,
                 json.dumps(["kp1"], ensure_ascii=False)),
                (2, "s_group1", 1000, 1300, "原始文本", json.dumps(["小明"]),
                 "主题A", json.dumps(["显卡"]), "摘要A", 0, 0, None),
                (3, "s_group1", 1000, 1400, "", "[]", "", "[]", "", 0, 0, None),
                (4, "s_group2", 1000, 1500, "", "[]", "主题B", "[]", "摘要B", 0, 0, None),
                (5, "missing", 1000, 1600, "", "[]", "主题C", "[]", "摘要C", 0, 0, None),
            ],
        )
        conn.executemany(
            "INSERT INTO memory_chest VALUES (?,?,?,?,?)",
            [
                (1, "宝盒A", "三元组内容", "s_group1", 0),
                (2, "宝盒B", "私聊知识", "s_dm", 0),
            ],
        )
        conn.executemany(
            "INSERT INTO jargon VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (1, "肥牛", "[]", "肥牛释义",
                 json.dumps([["s_group1", 1]], ensure_ascii=False), 1, 1, 1, None, 1,
                 None, None),
                (2, "无意义", "[]", None, "s_group2", 1, 1, None, None, 0, None, None),
                (3, "裸ID", "[]", "裸ID释义", "s_group2", 1, 1, 1, None, 1, None, None),
            ],
        )
        conn.executemany(
            "INSERT INTO thinking_back VALUES (?,?,?,?,?,?,?,?,?)",
            [
                (1, "s_group1", "Q1", "ctx", 1, "A1", "[]", 1700, 1700),
                (2, "s_group1", "Q2", "ctx", 1, "根据现有信息无法确认", "[]", 1710, 1710),
                (3, "s_dm", "Q3", "ctx", 1, "A3", "[]", 1720, 1720),
                (4, "s_group1", "Q4", "ctx", 0, "A4", "[]", 1730, 1730),
            ],
        )
        conn.executemany(
            "INSERT INTO memory_conflicts VALUES (?,?,?,?,?,?,?,?)",
            [
                (1, "年龄?", "20岁", 1800, 1800, None, "s_group1", 1),
                (2, "身高?", "", 1801, 1801, None, "s_group1", 1),
            ],
        )
        conn.commit()
    finally:
        conn.close()

    name_mapping = {
        "last_full_statistics": {
            "name_mapping": {
                "g100": ["一群别名", 1000],
                "u123": ["小明映射", 1000],
            }
        }
    }
    (source / "local_store.json").write_text(
        json.dumps(name_mapping, ensure_ascii=False), encoding="utf-8"
    )
    return source


@pytest.fixture
def source_dir(tmp_path):
    return _make_source(tmp_path)


@pytest.fixture
def env(tmp_path):
    data_dir = tmp_path / "kira"
    set_data_root(data_dir / "memory")
    ensure_directory_structure()
    index = MemoryIndex()
    store = TomlTreeStore(index=index)
    profiles = EntityProfileStore()
    yield data_dir, store, profiles
    store.close()


def _run(source_dir, store, profiles, **kwargs):
    kwargs.setdefault("now", 2000.0)
    options = ImportOptions(**kwargs)
    return asyncio.run(
        import_qanmocore(
            source_dir, tree_store=store, profile_store=profiles, options=options
        )
    )


def _entity_dir(data_dir: Path, entity_type: str, entity_id: str) -> Path:
    return data_dir / "memory" / "entities" / f"{entity_type}_{quote(entity_id, safe='')}"


def _read_toml(path: Path) -> dict:
    return tomllib.loads(path.read_text(encoding="utf-8"))


def _read_profile(data_dir: Path, entity_type: str, entity_id: str) -> dict:
    path = _entity_dir(data_dir, entity_type, entity_id) / "profile.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _files(root: Path, pattern: str) -> int:
    return sum(1 for _ in root.rglob(pattern))


# ── Mapping and counts ──────────────────────────────────────────────────────


def test_import_counts_and_manifest(source_dir, env):
    data_dir, store, profiles = env
    stats = _run(source_dir, store, profiles)

    assert stats.imported == {
        "points": 3,
        "history": 2,
        "chest": 2,
        "jargon": 2,
        "qa": 2,
        "conflicts": 1,
    }
    assert stats.updated == {}
    assert stats.profiles_written == 5
    assert stats.skipped["duplicate"] == 1
    assert stats.skipped["empty"] == 1
    assert stats.skipped["missing_stream"] == 1
    assert stats.skipped["jargon_no_meaning"] == 1
    assert stats.skipped["qa_unanswered"] == 1
    assert stats.skipped["qa_no_answer"] == 1
    assert stats.skipped["conflict_no_answer"] == 1
    assert stats.errors == []

    manifest_path = data_dir / "memory" / ".qanmocore_migrated"
    assert manifest_path.is_file()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["version"] == 1
    assert len(manifest["items"]) == 13
    assert manifest["items"]["history:2"] == {"duplicate_of": "history:1"}
    assert len(manifest["profiles"]) >= 5
    assert not list((data_dir / "memory").glob("*.json"))


def test_user_profile_content(source_dir, env):
    data_dir, store, profiles = env
    _run(source_dir, store, profiles)
    profile = _read_profile(data_dir, "user", "qq:123")

    assert profile["name"] == "小明"
    assert profile["nickname"] == "小明"
    assert "小明明" in profile["aliases"]
    assert "小明映射" in profile["aliases"]
    assert profile["platform"] == "qq"
    assert profile["interaction_count"] == 2
    assert profile["last_interaction"] == 1100.0
    assert profile["metadata"]["qanmocore"]["groups"] == {"100": "小明明"}
    assert len(profile["facts"]) == 3
    assert profile["facts"][0].startswith("[性格]")
    assert profile["facts"][1].startswith("[身份]")
    assert profile["facts"][2].startswith("[其他]")


def test_point_facts_and_importance(source_dir, env):
    data_dir, store, profiles = env
    _run(source_dir, store, profiles)
    facts_dir = _entity_dir(data_dir, "user", "qq:123") / "facts"
    files = sorted(facts_dir.glob("mai_point_*.toml"))
    assert len(files) == 3
    by_text = {_read_toml(path)["text"]: _read_toml(path) for path in files}
    assert by_text["小明喜欢猫"]["importance"] == 7
    assert by_text["小明是学生"]["importance"] == 6
    assert by_text["坏数据"]["importance"] == 4
    assert "qanmocore-import" in by_text["小明喜欢猫"]["tags"]
    assert "memory-point" in by_text["小明喜欢猫"]["tags"]


def test_group_profile_content(source_dir, env):
    data_dir, store, profiles = env
    _run(source_dir, store, profiles)
    profile = _read_profile(data_dir, "group", "qq:100")
    assert profile["name"] == "测试一群"
    assert "一群别名" in profile["aliases"]
    assert profile["last_interaction"] == 2000.0
    # Raw source counts: 3 chat_history rows + 3 thinking_back rows for s_group1.
    # This is an approximate profile counter, not the number of imported memories.
    assert profile["interaction_count"] == 6


def test_private_stream_user_profile_created(source_dir, env):
    data_dir, store, profiles = env
    _run(source_dir, store, profiles)
    profile = _read_profile(data_dir, "user", "qq:777")
    assert profile["nickname"] == "私聊用户"
    assert profile["name"] == "私聊用户"


# ── Source-specific texts ───────────────────────────────────────────────────


def test_history_becomes_group_reflection(source_dir, env):
    data_dir, store, profiles = env
    _run(source_dir, store, profiles)
    path = _entity_dir(data_dir, "group", "qq:100") / "reflections" / "mai_history_1.toml"
    data = _read_toml(path)
    assert data["type"] == "reflection"
    assert data["text"] == "主题A\n摘要A"
    assert data["importance"] == 5
    assert data["source"]["session"] == "qq:gm:100"
    assert data["source"]["key_points"] == ["kp1"]
    assert "chat-history" in data["tags"]
    assert "显卡" in data["tags"]


def test_chest_goes_to_stream_entity(source_dir, env):
    data_dir, store, profiles = env
    _run(source_dir, store, profiles)
    group_chest = _entity_dir(data_dir, "group", "qq:100") / "facts" / "mai_chest_1.toml"
    dm_chest = _entity_dir(data_dir, "user", "qq:777") / "facts" / "mai_chest_2.toml"
    assert _read_toml(group_chest)["text"] == "宝盒A\n三元组内容"
    assert _read_toml(dm_chest)["text"] == "宝盒B\n私聊知识"


def test_jargon_is_global_and_searchable(source_dir, env):
    data_dir, store, profiles = env
    _run(source_dir, store, profiles)
    path = data_dir / "memory" / "global" / "facts" / "mai_jargon_1.toml"
    data = _read_toml(path)
    assert data["text"] == "「肥牛」：肥牛释义"
    assert data["importance"] == 6
    assert data["source"]["stream_ids"] == ["s_group1"]

    meta = store.index.get_meta(
        "mai_jargon_1", entity_id="", entity_type="", folder="facts", base_dir="global"
    )
    assert meta is not None
    results = asyncio.run(
        store.search("肥牛", base_dir="global", folder="facts", k=5)
    )
    assert any("肥牛" in memory.text for memory in results)


def test_qa_and_conflicts_filtering(source_dir, env):
    data_dir, store, profiles = env
    _run(source_dir, store, profiles)
    facts = _entity_dir(data_dir, "group", "qq:100") / "facts"
    assert (facts / "mai_qa_1.toml").is_file()
    assert not (facts / "mai_qa_2.toml").exists()  # unanswered
    assert not (facts / "mai_qa_4.toml").exists()  # found_answer=0
    assert _read_toml(facts / "mai_qa_1.toml")["text"] == "Q：Q1\nA：A1"
    assert _read_toml(facts / "mai_conflict_1.toml")["text"] == "年龄?\n答：20岁"
    assert not (facts / "mai_conflict_2.toml").exists()

    user_facts = _entity_dir(data_dir, "user", "qq:777") / "facts"
    assert (user_facts / "mai_qa_3.toml").is_file()


# ── Options ─────────────────────────────────────────────────────────────────


def test_adapter_prefix_option(source_dir, env):
    data_dir, store, profiles = env
    _run(source_dir, store, profiles, adapter="onebot")
    assert (_entity_dir(data_dir, "user", "onebot:123") / "profile.json").is_file()
    assert (_entity_dir(data_dir, "group", "onebot:100") / "profile.json").is_file()


def test_include_filter_only_imports_history(source_dir, env):
    data_dir, store, profiles = env
    stats = _run(source_dir, store, profiles, include={"history"})
    assert stats.imported == {"history": 2}
    assert stats.profiles_written == 0
    assert not (data_dir / "memory" / "entities").exists() or not any(
        (data_dir / "memory" / "entities").rglob("profile.json")
    )
    assert not (data_dir / "memory" / "global" / "facts").exists() or not any(
        (data_dir / "memory" / "global" / "facts").glob("*.toml")
    )


def test_hippo_topics_are_opt_in(source_dir, env):
    data_dir, store, profiles = env
    hippo = source_dir / "hippo_memorizer"
    hippo.mkdir()
    (hippo / "s_group1.json").write_text(
        json.dumps(
            {
                "chat_id": "s_group1",
                "topics": {"话题A": {"messages": ["m1", "m2", "m3"]}},
                "current_batch": {},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    stats = _run(source_dir, store, profiles, include={"hippo-topics"})
    assert stats.imported == {"hippo-topics": 1}
    path = _entity_dir(data_dir, "group", "qq:100") / "facts"
    assert any(path.glob("mai_topic_*.toml"))


def test_dry_run_writes_nothing(source_dir, tmp_path):
    data_dir = tmp_path / "dry"
    set_data_root(data_dir / "memory")
    options = ImportOptions(now=2000.0, dry_run=True)
    stats = asyncio.run(import_qanmocore(source_dir, options=options))
    assert stats.dry_run is True
    assert stats.total_imported == 12
    assert not (data_dir / "memory").exists()
    assert not (data_dir / "memory" / ".qanmocore_migrated").exists()


# ── Idempotency / safety ────────────────────────────────────────────────────


def test_second_run_is_a_noop(source_dir, env):
    data_dir, store, profiles = env
    first = _run(source_dir, store, profiles)
    files_before = _files(data_dir / "memory", "*.toml")
    second = _run(source_dir, store, profiles)

    assert second.total_imported == 0
    assert second.total_updated == 0
    assert second.profiles_written == 0
    assert second.skipped["unchanged"] == first.total_imported
    assert _files(data_dir / "memory", "*.toml") == files_before


def test_source_change_updates_in_place(source_dir, env):
    data_dir, store, profiles = env
    _run(source_dir, store, profiles)
    path = _entity_dir(data_dir, "group", "qq:100") / "reflections" / "mai_history_1.toml"
    files_before = _files(data_dir / "memory", "*.toml")

    conn = sqlite3.connect(source_dir / "MaiBot.db")
    try:
        # Update both duplicate rows so the second one stays a duplicate in
        # the rerun and cannot be imported as a separate memory.
        conn.execute(
            "UPDATE chat_history SET summary = ? WHERE id IN (1, 2)", ("新摘要",)
        )
        conn.commit()
    finally:
        conn.close()

    stats = _run(source_dir, store, profiles)
    assert stats.updated["history"] == 1
    assert stats.total_imported == 0
    assert _read_toml(path)["text"] == "主题A\n新摘要"
    assert _files(data_dir / "memory", "*.toml") == files_before


def test_timeless_sources_stay_stable_across_runs(source_dir, env):
    data_dir, store, profiles = env
    _run(source_dir, store, profiles, now=2000.0)
    # Chest/jargon/most memory points have no source timestamp. The first run
    # persists a stable epoch in the manifest; a later wall clock must not make
    # them look updated (that would rewrite thousands of memories every run).
    stats = _run(source_dir, store, profiles, now=3000.0)
    assert stats.total_imported == 0
    assert stats.total_updated == 0
    assert stats.skipped["unchanged"] > 0


def test_deleted_memory_is_not_resurrected(source_dir, env):
    data_dir, store, profiles = env
    _run(source_dir, store, profiles)
    path = _entity_dir(data_dir, "group", "qq:100") / "reflections" / "mai_history_1.toml"
    path.unlink()

    stats = _run(source_dir, store, profiles)
    assert not path.exists()
    assert stats.skipped["deleted"] >= 1


def test_native_profile_fields_are_preserved(source_dir, env):
    data_dir, store, profiles = env

    async def save_native():
        profile = await profiles.get_profile("qq:123", "user")
        profile.name = "原生名"
        profile.facts = ["[身份] 原生事实"]
        await profiles.save_profile(profile)

    asyncio.run(save_native())
    _run(source_dir, store, profiles)

    profile = _read_profile(data_dir, "user", "qq:123")
    assert profile["name"] == "原生名"
    assert "[身份] 原生事实" in profile["facts"]
    assert any(fact.startswith("[性格]") for fact in profile["facts"])


def test_source_directory_is_untouched(source_dir, env):
    data_dir, store, profiles = env
    before = sorted(
        (path.relative_to(source_dir).as_posix(), path.stat().st_size)
        for path in source_dir.rglob("*")
        if path.is_file()
    )
    db_before = (source_dir / "MaiBot.db").stat().st_mtime_ns
    _run(source_dir, store, profiles)
    after = sorted(
        (path.relative_to(source_dir).as_posix(), path.stat().st_size)
        for path in source_dir.rglob("*")
        if path.is_file()
    )
    assert after == before
    assert (source_dir / "MaiBot.db").stat().st_mtime_ns == db_before
    assert not (source_dir / "MaiBot.db-wal").exists()
    assert not (source_dir / "MaiBot.db-shm").exists()


@pytest.mark.parametrize("chat_ids, expected", [
    ("s_group1", [("group", "qq:100")]),
    ("s_dm", [("user", "qq:777")]),
    (json.dumps([["s_group1", 1], ["s_dm", 2], ["missing", 1]]),
     [("group", "qq:100"), ("user", "qq:777")]),
    ("missing", []),
    ("", []),
])
def test_non_global_jargon_stays_in_source_entities(source_dir, env, chat_ids, expected):
    data_dir, store, profiles = env
    with sqlite3.connect(source_dir / "MaiBot.db") as conn:
        conn.execute(
            "UPDATE jargon SET is_global=0, chat_id=? WHERE id=1", (chat_ids,)
        )
    stats = _run(source_dir, store, profiles, include={"jargon"})
    assert stats.errors == []
    assert not (data_dir / "memory/global/facts/mai_jargon_1.toml").exists()
    paths = sorted((data_dir / "memory/entities").rglob("mai_jargon_1.toml"))
    assert paths == sorted(
        _entity_dir(data_dir, kind, entity_id) / "facts/mai_jargon_1.toml"
        for kind, entity_id in expected
    )
    global_matches = asyncio.run(store.search("肥牛", base_dir="global", folder="facts"))
    assert all("肥牛" not in memory.text for memory in global_matches)
    rerun = _run(source_dir, store, profiles, include={"jargon"})
    assert rerun.total_imported == rerun.total_updated == 0


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("manifest_text", [
    "{broken", "", "[]",
    '{"version": 99, "items": {}, "profiles": {}}',
    '{"version": 1, "items": [], "profiles": {}}',
    '{"version": 1, "items": {"history:1": null}, "profiles": {}}',
])
def test_invalid_manifest_aborts_without_writing(source_dir, env, dry_run, manifest_text):
    data_dir, store, profiles = env
    root = data_dir / "memory"
    path = root / ".qanmocore_migrated"
    path.write_text(manifest_text, encoding="utf-8")
    before = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    with pytest.raises(RuntimeError, match="manifest"):
        _run(source_dir, store, profiles, dry_run=dry_run)
    after = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    assert after == before


def test_explicit_empty_selection_imports_nothing(source_dir, env):
    data_dir, store, profiles = env
    assert ImportOptions().include == set(DEFAULT_INCLUDE)
    assert ImportOptions(include=set()).include == set()
    stats = _run(source_dir, store, profiles, include=set())
    assert stats.total_imported == stats.total_updated == stats.profiles_written == 0
    assert not list((data_dir / "memory").rglob("*.toml"))


@pytest.mark.parametrize("dry_run", [False, True])
def test_invalid_rows_do_not_use_per_chat_limit(source_dir, env, dry_run):
    data_dir, store, profiles = env
    stats = _run(
        source_dir, store, profiles, include={"history", "qa"},
        max_per_chat=1, dry_run=dry_run,
    )
    assert stats.imported == {"history": 2, "qa": 2}
    assert stats.errors == []
    if not dry_run:
        group = _entity_dir(data_dir, "group", "qq:100")
        assert (group / "reflections/mai_history_2.toml").is_file()
        assert (group / "facts/mai_qa_1.toml").is_file()


@pytest.mark.parametrize("field, value", [
    ("text", "手改内容"),
    ("importance", 10),
    ("tags", ["qanmocore-import", "manual-tag"]),
    ("type", "reflection"),
    ("source", {"origin": "manual"}),
])
@pytest.mark.parametrize("source_changed", [False, True])
@pytest.mark.parametrize("legacy_snapshot", [False, True])
def test_all_manual_content_edits_are_preserved(
    source_dir, env, field, value, source_changed, legacy_snapshot,
):
    data_dir, store, profiles = env
    _run(source_dir, store, profiles, include={"chest"})
    if legacy_snapshot:
        path = data_dir / "memory/.qanmocore_migrated"
        manifest = json.loads(path.read_text())
        for record in manifest["items"].values():
            record.pop("content_hash")
        path.write_text(json.dumps(manifest), encoding="utf-8")

    async def edit():
        memory = await store.get_memory("mai_chest_1", "qq:100", "group", "facts")
        setattr(memory, field, value)
        assert await store.update_memory(memory)

    asyncio.run(edit())
    path = _entity_dir(data_dir, "group", "qq:100") / "facts/mai_chest_1.toml"
    before = path.read_bytes()
    if source_changed:
        with sqlite3.connect(source_dir / "MaiBot.db") as conn:
            conn.execute("UPDATE memory_chest SET content='源更新' WHERE id=1")
    stats = _run(source_dir, store, profiles, include={"chest"})
    assert stats.skipped["edited"] == 1
    assert path.read_bytes() == before


def test_runtime_access_changes_do_not_block_source_updates(source_dir, env):
    data_dir, store, profiles = env
    _run(source_dir, store, profiles, include={"chest"})

    async def access():
        memory = await store.get_memory("mai_chest_1", "qq:100", "group", "facts")
        memory.meta.update(access_count=10, last_accessed=3000.0)
        assert await store.update_memory(memory)

    asyncio.run(access())
    with sqlite3.connect(source_dir / "MaiBot.db") as conn:
        conn.execute("UPDATE memory_chest SET content='源更新' WHERE id=1")
    stats = _run(source_dir, store, profiles, include={"chest"})
    assert stats.updated == {"chest": 1}


@pytest.mark.parametrize("detect_deletion_first", [False, True])
@pytest.mark.parametrize("force", [False, True])
def test_duplicate_rows_share_deletion_state_on_reordered_rerun(
    source_dir, env, detect_deletion_first, force,
):
    data_dir, store, profiles = env
    _run(source_dir, store, profiles, include={"history"})
    root = _entity_dir(data_dir, "group", "qq:100") / "reflections"
    original = root / "mai_history_1.toml"
    original.unlink()
    if detect_deletion_first:
        _run(source_dir, store, profiles, include={"history"})
    stats = _run(source_dir, store, profiles, include={"history"}, max_per_chat=1, force=force)
    assert stats.errors == []
    assert original.exists() is force
    assert not (root / "mai_history_2.toml").exists()
    if force:
        assert stats.imported == {"history": 1}
    else:
        assert stats.total_imported == 0


@pytest.mark.parametrize("first_limit, second_limit", [(None, 1), (1, None)])
def test_duplicate_rows_stay_shared_when_limit_changes(source_dir, env, first_limit, second_limit):
    data_dir, store, profiles = env
    _run(source_dir, store, profiles, include={"history"}, max_per_chat=first_limit)
    root = _entity_dir(data_dir, "group", "qq:100") / "reflections"
    before = sorted(p.name for p in root.glob("*.toml"))
    stats = _run(source_dir, store, profiles, include={"history"}, max_per_chat=second_limit)
    assert stats.total_imported == 0
    assert sorted(p.name for p in root.glob("*.toml")) == before


@pytest.mark.parametrize("deleted", [False, True])
def test_old_manifest_keeps_duplicate_ownership_on_upgrade(source_dir, env, deleted):
    data_dir, store, profiles = env
    _run(source_dir, store, profiles, include={"history"})
    path = data_dir / "memory/.qanmocore_migrated"
    manifest = json.loads(path.read_text())
    manifest["items"].pop("history:2")
    for record in manifest["items"].values():
        record.pop("dedupe_key")
        record.pop("content_hash")
    path.write_text(json.dumps(manifest), encoding="utf-8")
    root = _entity_dir(data_dir, "group", "qq:100") / "reflections"
    if deleted:
        (root / "mai_history_1.toml").unlink()
    stats = _run(source_dir, store, profiles, include={"history"}, max_per_chat=1)
    assert stats.total_imported == 0
    assert not (root / "mai_history_2.toml").exists()
    assert (root / "mai_history_1.toml").exists() is not deleted


@pytest.mark.parametrize("max_per_chat", [None, 2])
@pytest.mark.parametrize("ids", [(1,), (2,), (1, 2)])
def test_duplicate_source_rows_can_change_independently(source_dir, env, max_per_chat, ids):
    data_dir, store, profiles = env
    _run(source_dir, store, profiles, include={"history"})
    with sqlite3.connect(source_dir / "MaiBot.db") as conn:
        conn.executemany("UPDATE chat_history SET summary='新摘要' WHERE id=?", [(id_,) for id_ in ids])
    stats = _run(source_dir, store, profiles, include={"history"}, max_per_chat=max_per_chat)
    assert stats.errors == []
    root = _entity_dir(data_dir, "group", "qq:100") / "reflections"
    texts = sorted(_read_toml(path)["text"] for path in root.glob("*.toml"))
    expected = ["主题A\n新摘要"] if len(ids) == 2 else ["主题A\n新摘要", "主题A\n摘要A"]
    assert texts == sorted(expected)


@pytest.mark.parametrize("max_per_chat", [None, 2])
@pytest.mark.parametrize("deleted", [False, True])
def test_diverging_duplicate_keeps_user_protection(source_dir, env, max_per_chat, deleted):
    data_dir, store, profiles = env
    _run(source_dir, store, profiles, include={"history"})
    root = _entity_dir(data_dir, "group", "qq:100") / "reflections"
    path = root / "mai_history_1.toml"
    if deleted:
        path.unlink()
    else:
        path.write_text(path.read_text().replace("importance = 5", "importance = 10"))
    before = {p.name: p.read_bytes() for p in root.glob("*.toml")}
    with sqlite3.connect(source_dir / "MaiBot.db") as conn:
        conn.execute("UPDATE chat_history SET summary='新摘要' WHERE id=1")
    _run(source_dir, store, profiles, include={"history"}, max_per_chat=max_per_chat)
    assert {p.name: p.read_bytes() for p in root.glob("*.toml")} == before


@pytest.fixture
def cli_env(source_dir, tmp_path, monkeypatch):
    data_dir = tmp_path / "cli_target"
    monkeypatch.setattr(cli, "_load_importer", lambda *args: (
        lambda **kwargs: None, migration_qanmocore, memory_paths,
        memory_index, toml_tree_store, entity_profile,
    ))
    argv = ["--source", str(source_dir), "--data-dir", str(data_dir),
            "--kira-root", str(tmp_path)]
    return data_dir, argv


def test_cli_excluding_everything_imports_nothing(cli_env):
    data_dir, argv = cli_env
    assert cli.main(argv + ["--exclude", ",".join(DEFAULT_INCLUDE)]) == 0
    assert not list(data_dir.rglob("*.toml"))
    assert not list(data_dir.rglob("profile.json"))
    report = json.loads(next((data_dir / "migration_reports").glob("*.json")).read_text())
    assert report["options"]["include"] == []
    assert report["stats"]["imported"] == {}


def test_cli_report_uses_custom_nested_path(cli_env, tmp_path):
    data_dir, argv = cli_env
    path = tmp_path / "reports/nested/import.json"
    assert cli.main(argv + ["--include", "chest", "--report", str(path)]) == 0
    assert json.loads(path.read_text())["stats"]["imported"] == {"chest": 2}
    assert not (data_dir / "migration_reports").exists()


def test_cli_active_target_aborts_before_mutating(cli_env, monkeypatch, capsys):
    data_dir, argv = cli_env
    root = data_dir / "memory"
    root.mkdir(parents=True)
    wal = root / "memory_index.db-wal"
    wal.write_bytes(b"active-wal")
    importer = Mock(side_effect=AssertionError("must not import"))
    index = Mock(side_effect=AssertionError("must not open index"))
    bootstrap = Mock(side_effect=AssertionError("must not bootstrap"))
    monkeypatch.setattr(migration_qanmocore, "import_qanmocore", importer)
    monkeypatch.setattr(memory_index, "MemoryIndex", index)
    monkeypatch.setattr(cli, "_load_importer", bootstrap)
    assert cli.main(argv) == 3
    assert "--allow-running" in capsys.readouterr().err
    importer.assert_not_called()
    index.assert_not_called()
    bootstrap.assert_not_called()
    assert list(root.iterdir()) == [wal]
    assert wal.read_bytes() == b"active-wal"
    assert not (data_dir / "migration_backups").exists()


def test_cli_allow_running_explicitly_bypasses_guard(cli_env):
    data_dir, argv = cli_env
    root = data_dir / "memory"
    root.mkdir(parents=True)
    (root / "memory_index.db-wal").write_bytes(b"active-wal")
    assert cli.main(argv + ["--allow-running", "--include", "chest"]) == 0
    assert len(list(root.rglob("mai_chest_*.toml"))) == 2


@pytest.mark.parametrize("dry_run", [False, True])
def test_cli_invalid_manifest_reports_error_without_changes(cli_env, dry_run, capsys):
    data_dir, argv = cli_env
    root = data_dir / "memory"
    root.mkdir(parents=True)
    path = root / ".qanmocore_migrated"
    path.write_text("{broken", encoding="utf-8")
    assert cli.main(argv + (["--dry-run"] if dry_run else [])) == 3
    assert "manifest" in capsys.readouterr().err
    assert list(root.iterdir()) == [path]
    assert path.read_text() == "{broken"
