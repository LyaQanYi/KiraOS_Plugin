"""Runtime state: persistent hippocampus queue and recent-recall cache.

These port two MaiBot behaviours that KiraOS lacked:
- MaiBot persisted its unfinished topic/batch cache to disk; MemoryManager
  now persists _pending_conversations so a restart does not drop pre-hippocampus
  turns.
- MaiBot's thinking_back answered "what did I just remember" for ten
  minutes; RecallCache keeps one recent recall block per session and
  inject_context surfaces it as a [最近回忆] scope.
"""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

from core.chat.message_elements import Text
from plugins.KiraOS_Plugin.main import UserMemoryPlugin
from plugins.KiraOS_Plugin.memory.memory_manager import MemoryManager
from plugins.KiraOS_Plugin.memory.memory_paths import set_data_root
from plugins.KiraOS_Plugin.memory.pending_store import PENDING_FILE_NAME, PendingStore
from plugins.KiraOS_Plugin.memory.recall_cache import (
    RECALL_CACHE_FILE_NAME,
    RecallCache,
)


def _manager(data_root, **kwargs):
    set_data_root(data_root / "memory")
    defaults = dict(
        max_memory_length=20,
        hippocampus_threshold=99,
        llm_chat_timeout=30,
        enable_episode_memory=False,
        persist_hippocampus_pending=True,
    )
    defaults.update(kwargs)
    return MemoryManager(**defaults)


def _run(coro):
    return asyncio.run(coro)


def _event(text, user_id="123", adapter="onebot", session_type="dm", session_id="123"):
    message = SimpleNamespace(
        sender=SimpleNamespace(user_id=user_id, nickname="小明"),
        message=SimpleNamespace(chain=[Text(text)]),
    )
    return SimpleNamespace(
        adapter=SimpleNamespace(name=adapter),
        session=SimpleNamespace(session_type=session_type, session_id=session_id),
        messages=[message],
    )


# ── Pending queue ──────────────────────────────────────────────────────────


def test_pending_queue_is_persisted_and_restored(data_root):
    manager = _manager(data_root)
    try:
        chunk = [{"role": "user", "content": "还没到阈值的消息", "sender_id": "123"}]
        _run(manager.update_memory("onebot:dm:123", chunk))
        path = data_root / "memory" / PENDING_FILE_NAME
        assert path.is_file()
        data = json.loads(path.read_text(encoding="utf-8"))
        assert "onebot:dm:123" in data
    finally:
        manager.close()

    restored = _manager(data_root)
    try:
        assert "onebot:dm:123" in restored._pending_conversations
        assert restored._pending_conversations["onebot:dm:123"][0][0]["content"] == "还没到阈值的消息"
    finally:
        restored.close()


def test_pending_store_handles_corrupt_file(data_root, caplog):
    store = PendingStore(data_root / "memory" / PENDING_FILE_NAME)
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text("not json at all", encoding="utf-8")
    assert store.load() == {}
    assert not store.path.exists()
    assert list(store.path.parent.glob(PENDING_FILE_NAME + ".corrupt_*"))


def test_pending_persistence_can_be_disabled(data_root):
    manager = _manager(data_root, persist_hippocampus_pending=False)
    try:
        chunk = [{"role": "user", "content": "x"}]
        _run(manager.update_memory("onebot:dm:123", chunk))
        assert not (data_root / "memory" / PENDING_FILE_NAME).exists()
    finally:
        manager.close()


# ── Recall cache ───────────────────────────────────────────────────────────


def test_recall_cache_ttl_and_restore(tmp_path):
    path = tmp_path / RECALL_CACHE_FILE_NAME
    now = time.time()
    cache = RecallCache(path, ttl=10)
    cache.record("s1", "query one", "记忆 A", timestamp=now)
    assert cache.get("s1", timestamp=now + 5)["text"] == "记忆 A"
    assert cache.get("s1", timestamp=now + 11) is None

    cache.record("s2", "query two", "记忆 B", timestamp=now)
    restored = RecallCache(path, ttl=10)
    assert restored.get("s2", timestamp=now + 5)["text"] == "记忆 B"


def test_recent_recall_is_injected_on_a_later_query(data_root):
    manager = _manager(data_root)
    try:
        _run(
            manager.tree_store.add_memory(
                content_text="小明喜欢猫",
                memory_type="fact",
                importance=7,
                tags=["偏好"],
                entity_id="onebot:123",
                entity_type="user",
                folder="facts",
                semantic_id="likes_cats",
            )
        )
        plugin = UserMemoryPlugin(ctx=None, cfg={})
        plugin.memory_manager = manager
        plugin._registered_skill_names = []
        plugin._inject_profile = True
        plugin._inject_facts = True
        plugin._inject_reflections = True
        plugin._inject_group_memory = True
        plugin._inject_global_facts = False
        plugin._inject_episodes = False
        plugin._inject_recent_recall = True
        plugin._recall_top_k = 5
        plugin._max_recall_chars = 1500

        first = SimpleNamespace(system_prompt=[])
        _run(plugin.inject_context(_event("小明喜欢什么？"), first))
        first_text = chr(10).join(getattr(p, "content", "") for p in first.system_prompt)
        assert "小明喜欢猫" in first_text

        second = SimpleNamespace(system_prompt=[])
        _run(plugin.inject_context(_event("今天天气如何？"), second))
        second_text = chr(10).join(getattr(p, "content", "") for p in second.system_prompt)
        assert "[最近回忆]" in second_text
        assert "小明喜欢猫" in second_text
    finally:
        manager.close()
