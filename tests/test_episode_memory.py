"""Episode (event) memory tests.

QanMoCore's chat_history layer summarized whole conversation windows into
theme/summary/keywords/key_points. EpisodeManager ports that idea into KiraOS:
it buffers every message in a session (not only bot-triggered turns), asks the
extraction LLM for one compact event summary, and stores it as a normal TOML
memory under episodes/.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from urllib.parse import quote

try:
    import tomllib
except ImportError:  # Python 3.10
    import tomli as tomllib

from core.chat.message_elements import Text
from plugins.KiraOS_Plugin.main import UserMemoryPlugin
from plugins.KiraOS_Plugin.memory.memory_manager import MemoryManager
from plugins.KiraOS_Plugin.memory.memory_paths import set_data_root


SUMMARY = {
    "theme": "群友讨论显卡事件",
    "summary": "大家讨论了 A6000 和显卡价格，最后决定先租卡。",
    "keywords": ["显卡", "A6000", "租卡"],
    "participants": ["小明", "小红"],
}


class _FakeLLM:
    def __init__(self, response=None, error=None):
        self.response = json.dumps(SUMMARY, ensure_ascii=False) if response is None else response
        self.error = error
        self.calls = []

    async def chat(self, request):
        self.calls.append(request)
        if self.error is not None:
            raise self.error
        return SimpleNamespace(text_response=self.response)


def _manager(data_root, fake, **kwargs):
    set_data_root(data_root / "memory")
    defaults = dict(
        max_memory_length=20,
        hippocampus_threshold=99,
        llm_chat_timeout=30,
        llm_client=fake,
        enable_episode_memory=True,
        episode_threshold=5,
        episode_window_minutes=30,
        episode_require_bot=True,
    )
    defaults.update(kwargs)
    return MemoryManager(**defaults)


def _episode_dir(data_root, entity_type, entity_id):
    return (
        data_root
        / "memory"
        / "entities"
        / f"{entity_type}_{quote(entity_id, safe='')}"
        / "episodes"
    )


def _record_window(manager, session="onebot:gm:9001", with_bot=True):
    manager.episode_manager.min_messages = 3
    for index, text in enumerate(["小明：显卡好贵啊", "小红：A6000 现在多少钱"]):
        manager.record_episode_message(
            session,
            "user",
            text,
            sender_id=f"u{index}",
            sender_name=["小明", "小红"][index],
            timestamp=1000 + index,
        )
    if with_bot:
        manager.record_episode_message(
            session, "assistant", "先租卡试试吧，A6000 单卡也够用", timestamp=1003
        )


def test_episode_flush_stores_toml(data_root):
    fake = _FakeLLM()
    manager = _manager(data_root, fake)
    try:
        _record_window(manager)
        stored = asyncio.run(manager.episode_manager.flush_session("onebot:gm:9001"))
        assert stored is True
        files = list(_episode_dir(data_root, "group", "onebot:9001").glob("*.toml"))
        assert len(files) == 1
        data = tomllib.loads(files[0].read_text(encoding="utf-8"))
        assert data["type"] == "episode"
        assert "显卡" in data["text"]
        assert "episode" in data["tags"]
        assert data["source"]["session"] == "onebot:gm:9001"
        assert data["source"]["participants"] == ["小明", "小红"]
        assert data["source"]["message_count"] == 3
        assert data["importance"] == 6
        assert fake.calls, "episode summarizer should call the extraction LLM"
    finally:
        manager.close()


def test_episode_requires_bot_participation(data_root):
    fake = _FakeLLM()
    manager = _manager(data_root, fake)
    try:
        _record_window(manager, with_bot=False)
        stored = asyncio.run(manager.episode_manager.flush_session("onebot:gm:9001"))
        assert stored is False
        assert not list(_episode_dir(data_root, "group", "onebot:9001").glob("*.toml"))
    finally:
        manager.close()


def test_episode_duplicate_is_not_stored_twice(data_root):
    fake = _FakeLLM()
    manager = _manager(data_root, fake)
    try:
        _record_window(manager)
        asyncio.run(manager.episode_manager.flush_session("onebot:gm:9001"))
        files_before = list(_episode_dir(data_root, "group", "onebot:9001").glob("*.toml"))

        _record_window(manager)
        asyncio.run(manager.episode_manager.flush_session("onebot:gm:9001"))
        files_after = list(_episode_dir(data_root, "group", "onebot:9001").glob("*.toml"))
        assert len(files_after) == len(files_before)
    finally:
        manager.close()


def test_episode_buffers_survive_restart(data_root):
    fake = _FakeLLM()
    manager = _manager(data_root, fake)
    try:
        _record_window(manager)
        session = "onebot:gm:9001"
        assert manager.episode_manager._buffers[session]["messages"]
    finally:
        manager.close()

    restored = _manager(data_root, _FakeLLM())
    try:
        assert "onebot:gm:9001" in restored.episode_manager._buffers
        assert len(restored.episode_manager._buffers["onebot:gm:9001"]["messages"]) == 3
    finally:
        restored.close()


def test_episode_disabled_creates_no_manager(data_root):
    manager = _manager(data_root, _FakeLLM(), enable_episode_memory=False)
    try:
        assert manager.episode_manager is None
        manager.record_episode_message("onebot:gm:1", "user", "hello")
        assert not list((data_root / "memory").rglob("episode_*.toml"))
    finally:
        manager.close()


def test_plugin_hook_records_incoming_messages(data_root):
    fake = _FakeLLM()
    manager = _manager(data_root, fake)
    plugin = UserMemoryPlugin(ctx=None, cfg={})
    plugin.memory_manager = manager
    plugin._enable_episode_memory = True
    try:
        event = SimpleNamespace(
            adapter=SimpleNamespace(name="onebot"),
            session=SimpleNamespace(session_type="gm", session_id="9001"),
            message=SimpleNamespace(
                sender=SimpleNamespace(user_id="u1", nickname="小明"),
                self_id="bot",
                is_notice=False,
                chain=[Text("这是一条群聊消息")],
                timestamp=1000,
            ),
            timestamp=1000,
        )
        asyncio.run(plugin.record_episode_incoming(event))
        assert "onebot:gm:9001" in manager.episode_manager._buffers
        assert manager.episode_manager._buffers["onebot:gm:9001"]["messages"][0]["content"] == "这是一条群聊消息"
    finally:
        manager.close()


def test_episode_is_injected_into_group_prompt(data_root):
    fake = _FakeLLM()
    manager = _manager(data_root, fake)
    plugin = UserMemoryPlugin(ctx=None, cfg={})
    plugin.memory_manager = manager
    plugin._registered_skill_names = []
    plugin._inject_profile = True
    plugin._inject_facts = True
    plugin._inject_reflections = True
    plugin._inject_group_memory = True
    plugin._inject_global_facts = False
    plugin._inject_episodes = True
    plugin._recall_top_k = 5
    plugin._max_recall_chars = 1500
    try:
        _record_window(manager)
        asyncio.run(manager.episode_manager.flush_session("onebot:gm:9001"))

        message = SimpleNamespace(
            sender=SimpleNamespace(user_id="u1", nickname="小明"),
            message=SimpleNamespace(chain=[Text("还记得刚才的显卡事件吗？")]),
        )
        event = SimpleNamespace(
            adapter=SimpleNamespace(name="onebot"),
            session=SimpleNamespace(session_type="gm", session_id="9001"),
            messages=[message],
        )
        request = SimpleNamespace(system_prompt=[])
        asyncio.run(plugin.inject_context(event, request))
        text = chr(10).join(
            getattr(prompt, "content", "") for prompt in request.system_prompt
        )
        assert "[群聊相关记忆]" in text
        assert "显卡" in text
    finally:
        manager.close()
