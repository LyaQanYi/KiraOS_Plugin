"""Cross-adapter identity aliases.

One human can talk to the bot through several adapters. KiraOS keys memory by
"{adapter}:{platform_id}", so without an alias map WebChat, QQ and Telegram
would each get a separate profile and fact set. These tests pin the resolver,
its integration in storage/tools/hippocampus, the startup merge of pre-existing
alias data, and WebChat injecting the canonical QQ profile.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

import pytest
import tomli_w

from core.chat.message_elements import Text
from plugins.KiraOS_Plugin.main import UserMemoryPlugin
from plugins.KiraOS_Plugin import tools
from plugins.KiraOS_Plugin.memory import identity
from plugins.KiraOS_Plugin.memory.entity_profile import EntityProfile, EntityProfileStore
from plugins.KiraOS_Plugin.memory.identity_migration import reconcile_identity_aliases
from plugins.KiraOS_Plugin.memory.memory_index import MemoryIndex
from plugins.KiraOS_Plugin.memory.memory_manager import MemoryManager
from plugins.KiraOS_Plugin.memory.memory_paths import (
    ensure_directory_structure,
    get_entity_dir,
    get_entity_profile_path,
    set_data_root,
)
from plugins.KiraOS_Plugin.memory.toml_tree_store import TomlTreeStore


CANONICAL = "qq:1000000001"
ALIAS = "webchat:admin"


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def env(data_root):
    set_data_root(data_root / "memory")
    ensure_directory_structure()
    index = MemoryIndex()
    store = TomlTreeStore(index=index)
    profiles = EntityProfileStore()
    yield data_root, store, profiles
    store.close()


@pytest.fixture
def manager(data_root):
    set_data_root(data_root / "memory")
    mgr = MemoryManager(
        max_memory_length=20, hippocampus_threshold=99, llm_chat_timeout=30
    )
    yield mgr
    mgr.close()


def _entity_path(root: Path, entity_type: str, entity_id: str) -> Path:
    return root / "memory" / "entities" / f"{entity_type}_{quote(entity_id, safe='')}"


def _write_toml(path: Path, mem_id: str, text: str, *, importance: int = 5) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "id": mem_id,
        "type": "fact",
        "text": text,
        "importance": importance,
        "tags": ["qanmocore-import"],
        "source": {"session": "webchat:dm:admin", "time": "2026-01-01T00:00:00+00:00"},
    }
    path.write_bytes(tomli_w.dumps(payload).encode("utf-8"))


def _write_profile(
    path: Path,
    entity_id: str,
    *,
    name: str = "",
    nickname: str = "",
    facts=None,
    interaction_count: int = 0,
    last_interaction: float = 0.0,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    profile = EntityProfile(
        entity_id=entity_id,
        name=name,
        nickname=nickname,
        facts=list(facts or []),
        interaction_count=interaction_count,
        last_interaction=last_interaction,
    )
    path.write_text(json.dumps(profile.to_dict(), ensure_ascii=False), encoding="utf-8")


def _event(*, adapter="webchat", user_id="admin", session_type="dm", session_id="admin"):
    return SimpleNamespace(
        adapter=SimpleNamespace(name=adapter),
        session=SimpleNamespace(session_type=session_type, session_id=session_id),
        messages=[
            SimpleNamespace(
                sender=SimpleNamespace(user_id=user_id, nickname="管理员"),
                message=SimpleNamespace(chain=[Text("你好呀")]),
            )
        ],
    )


# ── Parser / resolver ───────────────────────────────────────────────────────


def test_parse_resolves_chains_and_drops_cycles():
    parsed = identity.parse_aliases(
        [
            "webchat:admin=qq:1000000001",
            "telegram:42=webchat:admin",
            "qq:1000000001=qq:1000000001",
            "not-an-entity",
        ]
    )
    assert parsed == {
        "telegram:42": "qq:1000000001",
        "webchat:admin": "qq:1000000001",
    }
    assert identity.parse_aliases(["a:1=b:2", "b:2=a:1"]) == {}


def test_resolve_only_affects_user_entities():
    identity.set_aliases(["webchat:admin=qq:1000000001"])
    assert identity.resolve_entity_id("webchat:admin", "user") == "qq:1000000001"
    assert identity.resolve_entity_id("webchat:admin", "") == "qq:1000000001"
    assert identity.resolve_entity_id("webchat:admin", "group") == "webchat:admin"
    assert identity.resolve_entity_id("qq:999", "user") == "qq:999"
    assert identity.resolve_entity_id("", "user") == ""


# ── Storage layer ──────────────────────────────────────────────────────────


def test_profile_store_writes_to_canonical_entity(env):
    root, store, profiles = env
    identity.set_aliases(["webchat:admin=qq:1000000001"])

    async def run():
        profile = await profiles.get_profile("webchat:admin", "user")
        assert profile.entity_id == "qq:1000000001"
        profile.name = "主号"
        assert await profiles.save_profile(profile)
        loaded = await profiles.get_profile("qq:1000000001", "user")
        assert loaded.name == "主号"

    _run(run())
    assert Path(get_entity_profile_path("qq:1000000001", "user")).is_file()
    assert not Path(get_entity_profile_path("webchat:admin", "user")).exists()


def test_toml_store_writes_reads_and_searches_canonical_entity(env):
    root, store, profiles = env
    identity.set_aliases(["webchat:admin=qq:1000000001"])

    async def run():
        memory = await store.add_memory(
            content_text="我住在南京",
            entity_id="webchat:admin",
            entity_type="user",
            folder="facts",
            semantic_id="lives_in_nanjing",
        )
        assert memory._entity_id == "qq:1000000001"
        got = await store.get_memory(
            "lives_in_nanjing", entity_id="webchat:admin", entity_type="user"
        )
        assert got is not None and got.text == "我住在南京"
        hits = await store.search(
            "南京", entity_id="webchat:admin", entity_type="user", folder="facts"
        )
        assert any(hit.text == "我住在南京" for hit in hits)

    _run(run())
    assert (Path(get_entity_dir("qq:1000000001", "user")) / "facts" / "lives_in_nanjing.toml").is_file()
    assert not Path(get_entity_dir("webchat:admin", "user")).exists()


# ── Manager / tools ────────────────────────────────────────────────────────


def test_session_and_fact_routing_resolve_alias(manager):
    identity.set_aliases(["webchat:admin=qq:1000000001"])
    assert manager._parse_entity_from_session("webchat:dm:admin") == (
        "qq:1000000001",
        "user",
    )
    assert manager._parse_entity_from_session("webchat:gm:group1") == (
        "webchat:group1",
        "group",
    )
    resolved = manager._resolve_fact_entity(
        {"speaker_id": "admin"},
        "webchat",
        {"admin": "admin"},
        ["admin"],
        "qq:1000000001",
        "user",
    )
    assert resolved == ("qq:1000000001", "user")


def test_tools_resolve_webchat_default_to_canonical(manager):
    identity.set_aliases(["webchat:admin=qq:1000000001"])
    event = _event()
    assert tools._resolve_entity(event, "", "") == (
        "qq:1000000001",
        "user",
    )
    assert tools._resolve_entity(event, "webchat:admin", "user") == (
        "qq:1000000001",
        "user",
    )
    # Explicit ids for other entities are preserved.
    assert tools._resolve_entity(event, "qq:999", "user") == ("qq:999", "user")


def test_extract_user_ids_returns_canonical(manager):
    identity.set_aliases(["webchat:admin=qq:1000000001"])
    assert UserMemoryPlugin._extract_user_ids(_event()) == ["qq:1000000001"]


def test_plugin_injection_in_webchat_uses_canonical_profile(data_root):
    set_data_root(data_root / "memory")
    ensure_directory_structure()
    identity.set_aliases(["webchat:admin=qq:1000000001"])
    mgr = MemoryManager(
        max_memory_length=20, hippocampus_threshold=99, llm_chat_timeout=30
    )

    async def seed():
        profile = await mgr.profile_store.get_profile("qq:1000000001", "user")
        profile.name = "本人"
        profile.facts = ["[身份] 测试用户"]
        await mgr.profile_store.save_profile(profile)

    _run(seed())
    plugin = UserMemoryPlugin(ctx=None, cfg={})
    plugin.memory_manager = mgr
    plugin._registered_skill_names = []
    plugin._inject_profile = True
    plugin._inject_facts = True
    plugin._inject_reflections = True
    plugin._inject_group_memory = True
    plugin._inject_global_facts = False
    plugin._recall_top_k = 5
    plugin._max_recall_chars = 1500
    request = SimpleNamespace(system_prompt=[])
    _run(plugin.inject_context(_event(), request))
    text = chr(10).join(getattr(prompt, "content", "") for prompt in request.system_prompt)
    assert "[用户 qq:1000000001 画像]" in text
    assert "本人" in text
    assert "[用户 webchat:admin 画像]" not in text
    mgr.close()


# ── Startup merge of pre-existing alias data ───────────────────────────────


def test_reconcile_moves_memories_and_merges_profiles(env):
    root, store, profiles = env
    identity.set_aliases(["webchat:admin=qq:1000000001"])

    alias_dir = _entity_path(root, "user", "webchat:admin")
    canon_dir = _entity_path(root, "user", "qq:1000000001")
    _write_profile(
        alias_dir / "profile.json",
        "webchat:admin",
        nickname="小号",
        facts=["alias fact"],
        interaction_count=2,
        last_interaction=100.0,
    )
    _write_profile(
        canon_dir / "profile.json",
        "qq:1000000001",
        name="主号",
        facts=["canonical fact"],
        interaction_count=3,
        last_interaction=50.0,
    )
    _write_toml(alias_dir / "facts" / "a.toml", "a", "alias memory")
    _write_toml(alias_dir / "facts" / "same.toml", "same", "duplicate text")
    _write_toml(canon_dir / "facts" / "same.toml", "same", "duplicate text")
    _write_toml(alias_dir / "facts" / "collide.toml", "collide", "alias colliding text")
    _write_toml(canon_dir / "facts" / "collide.toml", "collide", "canonical colliding text")

    stats = _run(reconcile_identity_aliases(profiles))
    assert stats["aliases"] == 1
    assert stats["profiles_merged"] == 1
    assert stats["files_moved"] == 2
    assert stats["duplicates_removed"] == 1
    assert not alias_dir.exists()

    profile = json.loads((canon_dir / "profile.json").read_text(encoding="utf-8"))
    assert profile["name"] == "主号"
    assert profile["nickname"] == "小号"
    assert profile["facts"] == ["canonical fact", "alias fact"]
    assert profile["interaction_count"] == 5
    assert profile["last_interaction"] == 100.0

    facts = {path.name for path in (canon_dir / "facts").glob("*.toml")}
    assert "a.toml" in facts
    assert "same.toml" in facts
    assert "collide.toml" in facts
    assert any(name.startswith("collide_") for name in facts)

    # Idempotent: nothing left to reconcile.
    again = _run(reconcile_identity_aliases(profiles))
    assert again["aliases"] == 0
    assert again["files_moved"] == 0


def test_reconcile_dry_run_changes_nothing(env):
    root, store, profiles = env
    identity.set_aliases(["webchat:admin=qq:1000000001"])
    alias_dir = _entity_path(root, "user", "webchat:admin")
    _write_toml(alias_dir / "facts" / "a.toml", "a", "alias memory")
    before = sorted(path.relative_to(root / "memory").as_posix() for path in (root / "memory").rglob("*") if path.is_file())

    stats = _run(reconcile_identity_aliases(profiles, dry_run=True))
    assert stats["aliases"] == 1
    assert stats["files_moved"] == 1
    after = sorted(path.relative_to(root / "memory").as_posix() for path in (root / "memory").rglob("*") if path.is_file())
    assert before == after
