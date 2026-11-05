"""Entity-id canonicalisation.

KiraOS stores entities as "{adapter}:{platform_id}" (qq:123456). KiraAI
events carry the bare platform id on sender.user_id. If the injection and
tool layers do not prefix the adapter, they read a different namespace than
the hippocampus writes, so imported profiles and facts never reach the LLM.

These tests pin the helper, the profile/recall lookup path, and the rule that
the raw sender id is still what feed_hippocampus puts into a chunk (otherwise
MemoryManager._resolve_fact_entity would produce qq:qq:123).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from plugins.KiraOS_Plugin.main import UserMemoryPlugin
from plugins.KiraOS_Plugin import tools


def _event(adapter_name="qq", user_id="123", session_type="gm", session_id="456"):
    sender = SimpleNamespace(user_id=user_id, nickname="小明")
    message = SimpleNamespace(sender=sender, message=SimpleNamespace(chain=[]))
    return SimpleNamespace(
        adapter=SimpleNamespace(name=adapter_name),
        session=SimpleNamespace(session_type=session_type, session_id=session_id),
        messages=[message],
    )


class _FakeManager:
    def __init__(self):
        self.calls = []

    async def recall(self, query, entity_id="", entity_type="user", k=5,
                     folders=None, base_dir="", **kwargs):
        self.calls.append(
            {
                "query": query,
                "entity_id": entity_id,
                "entity_type": entity_type,
                "folders": folders,
                "base_dir": base_dir,
                **kwargs,
            }
        )
        return []


# ── Helper ──────────────────────────────────────────────────────────────────


def test_prefixes_bare_platform_id():
    event = _event()
    assert UserMemoryPlugin._canonical_entity_id(event, "123") == "qq:123"


def test_preserves_already_canonical_id():
    event = _event()
    assert UserMemoryPlugin._canonical_entity_id(event, "qq:123") == "qq:123"


def test_empty_and_unknown_are_returned_as_is():
    event = _event()
    assert UserMemoryPlugin._canonical_entity_id(event, "") == ""
    assert UserMemoryPlugin._canonical_entity_id(event, None) == ""
    assert UserMemoryPlugin._canonical_entity_id(event, "unknown") == "unknown"


def test_no_adapter_returns_raw_id():
    event = _event(adapter_name="")
    assert UserMemoryPlugin._canonical_entity_id(event, "123") == "123"


def test_non_qq_adapter_uses_its_own_name():
    event = _event(adapter_name="onebot")
    assert UserMemoryPlugin._canonical_entity_id(event, "123") == "onebot:123"


# ── Injection helpers ───────────────────────────────────────────────────────


def test_extract_user_ids_returns_canonical_ids():
    event = _event()
    assert UserMemoryPlugin._extract_user_ids(event) == ["qq:123"]


def test_extract_user_ids_dedupes_raw_and_prefixed_spellings():
    first = SimpleNamespace(
        sender=SimpleNamespace(user_id="123", nickname="A"),
        message=SimpleNamespace(chain=[]),
    )
    second = SimpleNamespace(
        sender=SimpleNamespace(user_id="qq:123", nickname="A"),
        message=SimpleNamespace(chain=[]),
    )
    event = _event()
    event.messages = [first, second]
    assert UserMemoryPlugin._extract_user_ids(event) == ["qq:123"]


def test_primary_user_id_stays_raw_for_hippocampus_feed():
    event = _event()
    assert UserMemoryPlugin._get_primary_user_id(event) == "123"


# ── Tool entity resolution ─────────────────────────────────────────────────


def test_default_user_resolves_to_canonical():
    event = _event()
    assert tools._resolve_entity(event, "", "") == ("qq:123", "user")


def test_default_group_resolves_to_canonical_session_group():
    event = _event()
    assert tools._resolve_entity(event, "", "group") == ("qq:456", "group")


def test_explicit_bare_user_id_is_prefixed():
    event = _event()
    assert tools._resolve_entity(event, "789", "user") == ("qq:789", "user")


def test_explicit_canonical_id_is_unchanged():
    event = _event()
    assert tools._resolve_entity(event, "qq:789", "user") == ("qq:789", "user")


def test_global_entity_has_no_id_prefix():
    event = _event()
    assert tools._resolve_entity(event, "", "global") == ("", "global")


def test_private_session_group_default_is_empty():
    event = _event(session_type="dm", session_id="123")
    assert tools._resolve_entity(event, "", "group") == ("", "group")


# ── Cross-adapter explicit ids (WebChat -> imported QQ memory) ─────────────


def test_webchat_default_user_is_canonicalised():
    event = _event(adapter_name="webchat", session_type="dm", session_id="admin", user_id="admin")
    assert tools._resolve_entity(event, "", "") == ("webchat:admin", "user")


def test_webchat_can_target_an_explicit_qq_entity():
    event = _event(adapter_name="webchat", session_type="dm", session_id="admin", user_id="admin")
    assert tools._resolve_entity(event, "qq:1000000002", "user") == (
        "qq:1000000002",
        "user",
    )


def test_webchat_bare_id_still_uses_its_own_adapter():
    event = _event(adapter_name="webchat", session_type="dm", session_id="admin", user_id="admin")
    assert tools._resolve_entity(event, "1000000002", "user") == (
        "webchat:1000000002",
        "user",
    )


def test_memory_search_cross_adapter_ids_are_preserved():
    manager = _FakeManager()
    event = _event(adapter_name="webchat", session_type="dm", session_id="admin", user_id="admin")
    asyncio.run(
        tools.memory_search(manager, event, query="x", entity_id="qq:1,qq:2", k=3)
    )
    assert [call["entity_id"] for call in manager.calls] == ["qq:1", "qq:2"]


def test_memory_add_cross_adapter_entity_is_preserved(monkeypatch):
    captured = {}

    class _Store:
        async def add_memory(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(id="x")

    class _Manager:
        tree_store = _Store()
        extractor = None

    event = _event(adapter_name="webchat", session_type="dm", session_id="admin", user_id="admin")
    asyncio.run(
        tools.memory_add(
            _Manager(), event, text="补记一条 QQ 记忆", entity_id="qq:1000000002"
        )
    )
    assert captured["entity_id"] == "qq:1000000002"


# ── memory_search ──────────────────────────────────────────────────────────


def test_memory_search_canonicalises_comma_separated_ids():
    manager = _FakeManager()
    event = _event()
    asyncio.run(
        tools.memory_search(manager, event, query="x", entity_id="1, 2", k=3)
    )
    assert [call["entity_id"] for call in manager.calls] == ["qq:1", "qq:2"]


def test_memory_search_global_uses_base_dir_namespace():
    manager = _FakeManager()
    event = _event()
    asyncio.run(
        tools.memory_search(manager, event, query="肥牛", entity_type="global", k=3)
    )
    assert len(manager.calls) == 1
    call = manager.calls[0]
    assert call["entity_id"] == ""
    assert call["base_dir"] == "global"
    assert call["folders"] == ["facts"]


def test_memory_add_uses_canonical_entity(monkeypatch):
    captured = {}

    class _Store:
        async def add_memory(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(id="x")

    class _Manager:
        tree_store = _Store()
        extractor = None

    result = asyncio.run(
        tools.memory_add(
            _Manager(), _event(), text="用户喜欢猫", entity_id="123", entity_type="user"
        )
    )
    assert "Memory added" in result
    assert captured["entity_id"] == "qq:123"
    assert captured["entity_type"] == "user"
