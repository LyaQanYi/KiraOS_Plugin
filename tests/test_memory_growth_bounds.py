"""Bounds on memory growth: merge length cap, reflection merge gating and
profile compaction.

A LoCoMo run showed a few generic memories (e.g. "群聊氛围友善") grow to
~10-17KB: every "update" decision merged more text in, the merge prompt asked
to "keep all useful information", and the fallback concatenated without a
limit. These tests pin the caps that stop that.
"""

from __future__ import annotations

import asyncio

import pytest

from plugins.KiraOS_Plugin.memory import memory_extractor as mx
from plugins.KiraOS_Plugin.memory.entity_profile import EntityProfileStore
from plugins.KiraOS_Plugin.memory.memory_extractor import MemoryExtractor
from plugins.KiraOS_Plugin.memory.memory_index import MemoryIndex
from plugins.KiraOS_Plugin.memory.memory_paths import (
    set_data_root,
    ensure_directory_structure,
    get_index_db_path,
)
from plugins.KiraOS_Plugin.memory.toml_tree_store import TomlTreeStore

CAP = mx._MERGE_MAX_CHARS


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class ScriptedClient:
    """Fake LLM client: answers each prompt via ``respond(prompt)``."""

    def __init__(self, respond):
        self.respond = respond
        self.prompts: list[str] = []

    async def chat(self, request, **kwargs):
        # chat_text builds LLMRequest(messages=[...]), but conftest's stub
        # has .messages defaulting to [] rather than being a required argument,
        # so we can't be sure it's set; prefer getattr.
        msgs = getattr(request, "messages", [])
        prompt = msgs[-1]["content"] if msgs else str(request)
        self.prompts.append(prompt)
        return type("R", (), {"text_response": self.respond(prompt)})()


@pytest.fixture
def store(tmp_path):
    set_data_root(tmp_path / "memory")
    ensure_directory_structure()
    index = MemoryIndex(db_path=get_index_db_path())
    s = TomlTreeStore(index=index)
    yield s
    index.close()


# ── merge_facts ──────────────────────────────────────────────────────────────

def test_llm_merge_result_is_clipped(store):
    client = ScriptedClient(lambda p: "很长" * 500)
    ex = MemoryExtractor(store, extraction_client=client, reflection_client=client)
    merged = _run(ex.merge_facts("旧" * 50, "新" * 50))
    assert len(merged) <= CAP
    assert "40 字以内" in client.prompts[0]
    assert "保留所有有用信息" not in client.prompts[0]


@pytest.mark.parametrize("with_client", [False, True])
def test_fallback_merge_stays_bounded_over_repeated_updates(store, with_client):
    """Repeated fallback merges must never grow past the cap, and must keep
    the newest information."""
    client = ScriptedClient(lambda p: "") if with_client else None  # empty → fallback
    ex = MemoryExtractor(store, extraction_client=client, reflection_client=client)
    text = "群聊氛围积极友善"
    for i in range(60):
        text = _run(ex.merge_facts(text, f"第{i}次补充：大家互相鼓励分享生活"))
        assert len(text) <= CAP
    assert text.endswith("第59次补充：大家互相鼓励分享生活")


def test_fallback_short_inputs_unchanged():
    assert mx._merge_fallback("旧文本", "新文本") == "旧文本；新文本"
    assert mx._merge_fallback("", "新") == "新"
    assert len(mx._merge_fallback("a" * 10, "b" * 500)) <= CAP


# ── merged tags ──────────────────────────────────────────────────────────────

TAG_CAP = mx._MERGE_MAX_TAGS


def test_merge_tags_caps_and_prefers_new():
    old = [f"old{i}" for i in range(30)]
    new = [f"new{i}" for i in range(5)]
    merged = mx._merge_tags(old, new)
    assert len(merged) == TAG_CAP
    assert set(new) <= set(merged)
    assert merged == sorted(merged)
    # Small sets: plain union, junk dropped, no duplicates.
    assert mx._merge_tags(["a", None, "b"], ["b", "", "c"]) == ["a", "b", "c"]
    # Oversized new set alone is still capped.
    assert len(mx._merge_tags([], [f"t{i}" for i in range(50)])) == TAG_CAP


def test_update_merge_keeps_tags_bounded(store):
    """Repeated "update" merges must not grow tags past the cap, and an
    oversized memory from an older version is trimmed on its next merge."""
    client = ScriptedClient(lambda p: "群聊氛围友善")
    ex = MemoryExtractor(store, extraction_client=client, reflection_client=client)
    mem = _run(store.add_memory(
        "群聊氛围友善", tags=[f"legacy{i}" for i in range(136)],
        entity_id="locomo:g1", entity_type="group",
    ))

    async def always_update(*args, **kwargs):
        current = await store.get_all_memories("locomo:g1", "group", "facts")
        return "update", current[0]

    ex.deduplicate = always_update
    for i in range(10):
        _run(ex.deduplicate_and_store(
            {"content": f"补充{i}", "tags": [f"r{i}a", f"r{i}b", f"r{i}c"]},
            "locomo:g1", "group",
        ))
        stored = _run(store.get_all_memories("locomo:g1", "group", "facts"))
        assert len(stored) == 1
        assert len(stored[0].tags) <= TAG_CAP
    assert {"r9a", "r9b", "r9c"} <= set(stored[0].tags)
    assert stored[0].id == mem.id


# ── reflection merge gating ─────────────────────────────────────────────────

def _seed_facts(store, n=6):
    for i in range(n):
        _run(store.add_memory(
            content_text=f"小明喜欢运动项目{i}", memory_type="fact", importance=6,
            entity_id="qq:1", entity_type="user", folder="facts",
            semantic_id=f"f{i}",
        ))


def _reflection_client(insight: str, decision: str):
    def respond(prompt):
        if "更高层面的洞察" in prompt:
            return insight
        if "判断它们的关系" in prompt:
            return decision
        if "snake_case" in prompt or "ID" in prompt:
            return "new_reflection"
        return "合并结果"
    return ScriptedClient(respond)


def _reflections(store):
    return _run(store.get_all_memories(
        entity_id="qq:1", entity_type="user", folder="reflections"
    ))


@pytest.mark.parametrize("decision,expected_count,expected_text", [
    ("new", 2, None),
    ("duplicate", 1, "小明热爱运动"),
    ("update", 1, "合并结果"),
])
def test_reflection_merge_runs_conflict_check(store, decision, expected_count, expected_text):
    _seed_facts(store)
    _run(store.add_memory(
        content_text="小明热爱运动", memory_type="reflection", importance=7,
        entity_id="qq:1", entity_type="user", folder="reflections",
        semantic_id="loves_sports",
    ))
    client = _reflection_client("小明热爱运动并且喜欢独处", decision)
    ex = MemoryExtractor(store, extraction_client=client, reflection_client=client)
    ex.reflection_threshold = 3

    _run(ex.generate_reflections("qq:1", "user"))

    refl = _reflections(store)
    assert len(refl) == expected_count
    assert any("判断它们的关系" in p for p in client.prompts)
    if expected_text is not None:
        assert refl[0].text == expected_text


# ── profile compaction ──────────────────────────────────────────────────────

def _compact_client(output: str):
    return ScriptedClient(lambda p: output if "画像事实列表" in p else "")


def test_summarize_rejects_untagged_output(store):
    ex = MemoryExtractor(store, reflection_client=_compact_client("随便说点什么"))
    assert _run(ex.summarize_profile_facts(["a", "b"])) == []


def test_summarize_accepts_tagged_output_and_caps(store):
    out = "\n".join(f"[其他] 事实{i}" for i in range(20))
    ex = MemoryExtractor(store, reflection_client=_compact_client(out))
    got = _run(ex.summarize_profile_facts(["a"], max_facts=5))
    assert got == [f"[其他] 事实{i}" for i in range(5)]


def test_apply_compacted_keeps_concurrent_appends(store):
    profiles = EntityProfileStore()
    for f in ["旧1", "旧2"]:
        _run(profiles.add_fact("qq:1", f, "user"))
    snapshot = ["旧1", "旧2"]
    _run(profiles.add_fact("qq:1", "并发新增", "user"))

    updated = _run(profiles.apply_compacted_facts(
        "qq:1", "user", snapshot, ["[其他] 压实"]
    ))
    assert updated.facts == ["[其他] 压实", "并发新增"]


def test_apply_compacted_aborts_when_snapshot_fact_removed(store):
    profiles = EntityProfileStore()
    for f in ["旧1", "旧2"]:
        _run(profiles.add_fact("qq:1", f, "user"))
    _run(profiles.remove_fact("qq:1", "旧1", "user"))
    assert _run(profiles.apply_compacted_facts(
        "qq:1", "user", ["旧1", "旧2"], ["[其他] 压实"]
    )) is None
    assert _run(profiles.get_profile("qq:1", "user")).facts == ["旧2"]


def test_apply_compacted_never_creates_missing_profile(store):
    profiles = EntityProfileStore()
    assert _run(profiles.apply_compacted_facts("qq:9", "user", [], ["[其他] x"])) is None


@pytest.fixture
def manager(tmp_path):
    from plugins.KiraOS_Plugin.memory.memory_manager import MemoryManager
    set_data_root(tmp_path / "memory")
    mgr = MemoryManager(max_memory_length=20, hippocampus_threshold=1, llm_chat_timeout=30)
    yield mgr
    mgr.close()


def _bloat(mgr, n=15):
    for i in range(n):
        _run(mgr.profile_store.add_fact("qq:1", f"小明喜欢第{i}种咖啡", "user"))


def test_post_write_compaction_shrinks_profile(manager):
    out = "[技能兴趣] 小明喜欢咖啡\n[性格] 小明很外向"
    client = _compact_client(out)
    manager.set_llm_clients(client, client)
    _bloat(manager, 14)

    _run(manager._update_profile_from_facts(
        "qq:1", "user", [{"content": "小明每天跑步", "importance": 8}]
    ))
    assert _run(manager.profile_store.get_profile("qq:1", "user")).facts == [
        "[技能兴趣] 小明喜欢咖啡", "[性格] 小明很外向",
    ]


def test_compaction_below_threshold_is_noop(manager):
    client = _compact_client("[其他] 不应出现")
    manager.set_llm_clients(client, client)
    _bloat(manager, 3)
    assert _run(manager.compact_profile("qq:1", "user")) is False
    assert client.prompts == []


def test_compaction_keeps_original_on_bad_output(manager):
    client = _compact_client("抱歉，我无法完成")
    manager.set_llm_clients(client, client)
    _bloat(manager)
    before = _run(manager.profile_store.get_profile("qq:1", "user")).facts
    assert _run(manager.compact_profile("qq:1", "user")) is False
    assert _run(manager.profile_store.get_profile("qq:1", "user")).facts == before


def test_compacted_output_does_not_retrigger(manager):
    out = "\n".join(f"[其他] 事实{i}" for i in range(30))
    client = _compact_client(out)
    manager.set_llm_clients(client, client)
    _bloat(manager)
    assert _run(manager.compact_profile("qq:1", "user")) is True
    profile = _run(manager.profile_store.get_profile("qq:1", "user"))
    assert not manager._profile_needs_compaction(profile)


def test_forgetting_sweep_compacts_and_skips_profileless_entities(manager, tmp_path):
    from plugins.KiraOS_Plugin.memory.memory_paths import (
        ensure_entity_dirs, get_entity_profile_path,
    )
    client = _compact_client("[其他] 压实结果")
    manager.set_llm_clients(client, client)
    _bloat(manager)
    ensure_entity_dirs("qq:2", "user")  # entity dir with no profile.json

    _run(manager.run_forgetting_cycle())

    assert _run(manager.profile_store.get_profile("qq:1", "user")).facts == ["[其他] 压实结果"]
    import os
    assert not os.path.exists(get_entity_profile_path("qq:2", "user"))


def test_compaction_honors_configured_thresholds(tmp_path):
    """profile_compact_threshold / _max_chars from the plugin config apply."""
    from plugins.KiraOS_Plugin.memory.memory_manager import MemoryManager
    set_data_root(tmp_path / "memory")
    mgr = MemoryManager(profile_compact_threshold=4, profile_compact_max_chars=1)
    try:
        assert mgr.profile_compact_max_chars == 200  # clamped to schema minimum
        client = _compact_client("[其他] 压实")
        mgr.set_llm_clients(client, client)
        _bloat(mgr, 4)
        assert _run(mgr.compact_profile("qq:1", "user")) is True
        assert _run(mgr.profile_store.get_profile("qq:1", "user")).facts == ["[其他] 压实"]
    finally:
        mgr.close()
