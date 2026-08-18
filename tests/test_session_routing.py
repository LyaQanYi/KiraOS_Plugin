"""Session-type routing.

KiraAI's `MessageType` enum has three values (core/chat/message_utils.py:32-34):
`gm` group, `dm` direct, `sm` system. KiraOS previously treated "anything that
isn't gm" as a direct message, so a system-message session would mint a user
entity and leave a directory under `entities/` that corresponds to no real
person. The host's own builtin plugin whitelists `{"dm", "gm"}`
(core/plugin/builtin_plugins/session_tools/main.py:91); we now match it.

The guard has to sit at the buffering entry point rather than deep in
`_hippocampus_process`, because that function's catch-all handler re-buffers
the batch for retry — raising there would make an `sm` batch retry forever
instead of being dropped.
"""

from __future__ import annotations

import asyncio

import pytest

from plugins.KiraOS_Plugin.memory.memory_paths import set_data_root
from plugins.KiraOS_Plugin.memory.memory_manager import MemoryManager


@pytest.fixture
def manager(tmp_path):
    set_data_root(tmp_path / "memory")
    return MemoryManager(
        max_memory_length=20, hippocampus_threshold=1, llm_chat_timeout=30
    )


# ── Parsing ──────────────────────────────────────────────────────────────────

def test_direct_message_parses_to_user():
    eid, etype = MemoryManager._parse_entity_from_session("onebot:dm:12345")
    assert (eid, etype) == ("onebot:12345", "user")


def test_group_message_parses_to_group():
    eid, etype = MemoryManager._parse_entity_from_session("onebot:gm:98765")
    assert (eid, etype) == ("onebot:98765", "group")


def test_system_message_is_rejected():
    """The bug: `sm` used to fall through to the user branch."""
    with pytest.raises(ValueError, match="Non-conversational"):
        MemoryManager._parse_entity_from_session("onebot:sm:system")


def test_unknown_session_type_is_rejected():
    with pytest.raises(ValueError, match="Non-conversational"):
        MemoryManager._parse_entity_from_session("onebot:zz:whatever")


def test_malformed_session_is_rejected():
    with pytest.raises(ValueError, match="Invalid session ID"):
        MemoryManager._parse_entity_from_session("onebot:dm")


def test_id_containing_colons_is_preserved():
    """maxsplit=2 keeps colons in the id itself (some adapters use them)."""
    eid, etype = MemoryManager._parse_entity_from_session("discord:dm:guild:123")
    assert (eid, etype) == ("discord:guild:123", "user")


# ── Buffering ────────────────────────────────────────────────────────────────

def test_system_session_never_enters_the_pending_queue(manager):
    """Must be dropped before buffering, or the retry handler loops on it."""
    chunk = [{"role": "user", "content": "系统消息", "sender_id": "", "sender_name": ""}]
    manager._buffer_for_hippocampus("onebot:sm:system", chunk)
    assert manager._pending_conversations.get("onebot:sm:system") is None


def test_conversational_session_does_enter_the_pending_queue(manager):
    """Guard must not be over-broad — normal traffic still flows."""
    manager._hippocampus_threshold = 99  # keep it queued, don't dispatch
    chunk = [{"role": "user", "content": "你好", "sender_id": "1", "sender_name": "A"}]
    manager._buffer_for_hippocampus("onebot:dm:12345", chunk)
    assert manager._pending_conversations.get("onebot:dm:12345") == [chunk]


def test_update_memory_skips_hippocampus_for_system_sessions(manager):
    """End-to-end through the public feed entry point."""
    chunk = [{"role": "user", "content": "x", "sender_id": "", "sender_name": ""}]
    asyncio.run(manager.update_memory("onebot:sm:system", chunk))

    # Short-term sliding window still records it (harmless, in-memory), but
    # nothing is queued for entity extraction.
    assert not manager._pending_conversations.get("onebot:sm:system")


def test_is_conversational_session_helper(manager):
    assert manager._is_conversational_session("onebot:dm:1") is True
    assert manager._is_conversational_session("onebot:gm:1") is True
    assert manager._is_conversational_session("onebot:sm:1") is False
    assert manager._is_conversational_session("garbage") is False
