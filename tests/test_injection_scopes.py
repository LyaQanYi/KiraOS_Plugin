"""Injection scopes for group and global memories.

Before this change inject_context only looked at the current speaker's user
profile and user recall. Imported group summaries and global knowledge were
written but never reached the LLM. These tests pin the new group/global blocks,
the switches that disable them, and the recall/base_dir plumbing.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from core.chat.message_elements import Text
from plugins.KiraOS_Plugin.main import UserMemoryPlugin
from plugins.KiraOS_Plugin import tools
from plugins.KiraOS_Plugin.memory.memory_manager import MemoryManager
from plugins.KiraOS_Plugin.memory.memory_paths import set_data_root


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def manager(data_root):
    set_data_root(data_root / "memory")
    mgr = MemoryManager(
        max_memory_length=20, hippocampus_threshold=99, llm_chat_timeout=30
    )
    _seed(mgr)
    yield mgr
    mgr.close()


def _seed(manager: MemoryManager):
    async def seed():
        profile = await manager.profile_store.get_profile("qq:123", "user")
        profile.name = "小明"
        profile.nickname = "小明"
        profile.facts = ["[身份] 小明是学生"]
        await manager.profile_store.save_profile(profile)

        group = await manager.profile_store.get_profile("qq:456", "group")
        group.name = "测试群"
        await manager.profile_store.save_profile(group)

        await manager.tree_store.add_memory(
            content_text="小明喜欢猫",
            memory_type="fact",
            importance=7,
            tags=["qanmocore-import", "技能兴趣"],
            entity_id="qq:123",
            entity_type="user",
            folder="facts",
            semantic_id="likes_cats",
        )
        await manager.tree_store.add_memory(
            content_text="群聊讨论了显卡和 A6000",
            memory_type="reflection",
            importance=5,
            tags=["qanmocore-import", "chat-history"],
            entity_id="qq:456",
            entity_type="group",
            folder="reflections",
            semantic_id="gpu_talk",
        )
        await manager.tree_store.add_memory(
            content_text="黑话「肥牛」：指某个 AI 角色",
            memory_type="fact",
            importance=6,
            tags=["qanmocore-import", "jargon"],
            entity_id="",
            entity_type="",
            folder="facts",
            base_dir="global",
            semantic_id="jargon_fei_niu",
        )

    _run(seed())


def _plugin(manager: MemoryManager, **flags) -> UserMemoryPlugin:
    plugin = UserMemoryPlugin(ctx=None, cfg={})
    plugin.memory_manager = manager
    plugin._registered_skill_names = []
    plugin._recall_top_k = 5
    plugin._max_recall_chars = 1500
    plugin._inject_profile = flags.get("inject_profile", True)
    plugin._inject_facts = flags.get("inject_facts", True)
    plugin._inject_reflections = flags.get("inject_reflections", True)
    plugin._inject_group_memory = flags.get("inject_group_memory", True)
    plugin._inject_global_facts = flags.get("inject_global_facts", True)
    return plugin


def _event(*, session_type="gm", session_id="456", user_id="123", text="肥牛和显卡是什么"):
    message = SimpleNamespace(
        sender=SimpleNamespace(user_id=user_id, nickname="小明"),
        message=SimpleNamespace(chain=[Text(text)]),
    )
    return SimpleNamespace(
        adapter=SimpleNamespace(name="qq"),
        session=SimpleNamespace(session_type=session_type, session_id=session_id),
        messages=[message],
    )


def _inject(plugin: UserMemoryPlugin, event) -> str:
    request = SimpleNamespace(system_prompt=[])
    _run(plugin.inject_context(event, request))
    return "\n".join(getattr(prompt, "content", "") for prompt in request.system_prompt)


# ── Group sessions ──────────────────────────────────────────────────────────


def test_group_session_injects_user_group_and_global_blocks(manager):
    text = _inject(_plugin(manager), _event())
    assert "[用户 qq:123 画像]" in text
    assert "小明是学生" in text
    assert "[群 qq:456 画像]" in text
    assert "测试群" in text
    assert "[群聊相关记忆]" in text
    assert "显卡" in text
    assert "[通用知识]" in text
    assert "肥牛" in text


def test_direct_session_has_no_group_blocks(manager):
    text = _inject(_plugin(manager), _event(session_type="dm", session_id="123"))
    assert "[用户 qq:123 画像]" in text
    assert "[群 qq:456 画像]" not in text
    assert "[群聊相关记忆]" not in text
    assert "[通用知识]" in text


def test_group_and_global_switches_disable_blocks(manager):
    plugin = _plugin(manager, inject_group_memory=False, inject_global_facts=False)
    text = _inject(plugin, _event())
    assert "[群 qq:456 画像]" not in text
    assert "[群聊相关记忆]" not in text
    assert "[通用知识]" not in text
    # User injection is unaffected.
    assert "[用户 qq:123 画像]" in text


def test_fact_switch_filters_user_facts(manager):
    plugin = _plugin(manager, inject_facts=False)
    text = _inject(plugin, _event())
    assert "小明喜欢猫" not in text
    # Reflections remain enabled.
    assert "显卡" in text


def test_max_recall_chars_keeps_every_scope(manager):
    plugin = _plugin(manager)
    plugin._max_recall_chars = 200
    text = _inject(plugin, _event())
    # The budget is split across scopes, so a long user block cannot push the
    # group/global sections out of the prompt.
    assert "[群聊相关记忆]" in text
    assert "[通用知识]" in text


def test_build_recall_text_truncates_long_lines(manager):
    plugin = _plugin(manager)
    plugin._max_recall_chars = 200
    text = plugin._build_recall_text(
        [("[a]", ["x" * 500]), ("[b]", ["y" * 500])]
    )
    assert "[a]" in text
    assert "[b]" in text
    assert "…" in text
    assert "记忆已截断" in text


# ── Recall plumbing ─────────────────────────────────────────────────────────


def test_recall_supports_global_base_dir(manager):
    memories = _run(
        manager.recall(
            query="肥牛",
            entity_id="",
            entity_type="",
            folders=["facts"],
            base_dir="global",
            k=5,
        )
    )
    assert any("肥牛" in memory.text for memory in memories)


def test_recall_requires_entity_or_base_dir(manager):
    assert _run(manager.recall("anything")) == []


def test_memory_search_global_returns_global_knowledge(manager):
    result = _run(
        tools.memory_search(
            manager, _event(), query="肥牛", entity_type="global", k=5
        )
    )
    assert "肥牛" in result
