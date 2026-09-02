"""MemoryExtractor timeout-path tests.

Every LLM call in the extractor is wrapped in `asyncio.wait_for(...)`. When the
provider is slow, that raises `asyncio.TimeoutError`, whose `str(e)` is the
empty string -- which is why a real-world log line reads:

    ERROR [memory_extractor] Merge facts error:

with nothing after the colon. These tests pin the *behaviour* behind each of
those timeouts (every path degrades instead of propagating) and, for the two
that log, that the message now names the timeout instead of rendering blank.

A fake client stands in for the host LLM, so the suite stays hermetic -- no
network, no API key. Timeout is set to 0.05s so the slow-path tests are fast.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from plugins.KiraOS_Plugin.memory.memory_extractor import MemoryExtractor
from plugins.KiraOS_Plugin.memory.memory_index import MemoryIndex
from plugins.KiraOS_Plugin.memory.memory_paths import (
    set_data_root,
    ensure_directory_structure,
    get_index_db_path,
)
from plugins.KiraOS_Plugin.memory.toml_tree_store import TomlTreeStore


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class FakeLLMClient:
    """Stands in for the host LLMModelClient.

    `chat_text()` reads `.text_response` off the returned object, so the fake
    response mirrors that attribute name rather than a plain `.text`.
    """

    def __init__(self, *, delay: float = 0.0, text: str = ""):
        self.delay = delay
        self.text = text
        self.calls = 0

    async def chat(self, request, **kwargs):
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        return type("FakeResponse", (), {"text_response": self.text})()


@pytest.fixture
def rooted(tmp_path):
    set_data_root(tmp_path / "memory")
    ensure_directory_structure()
    return tmp_path / "memory"


@pytest.fixture
def store(rooted):
    index = MemoryIndex(db_path=get_index_db_path())
    s = TomlTreeStore(index=index)
    yield s
    index.close()


@pytest.fixture
def extractor(store):
    """Extractor with a 50ms budget, so 'slow' clients need only 200ms."""
    return MemoryExtractor(store, llm_chat_timeout=0.05)


SLOW = 0.2   # comfortably over the 0.05s budget
FAST = 0.0


# ── merge_facts ──────────────────────────────────────────────────────────────

def test_merge_facts_falls_back_to_concatenation_on_timeout(extractor):
    """The observed failure: merge times out, the two texts get joined raw."""
    client = FakeLLMClient(delay=SLOW, text="一条漂亮的合并结果")
    extractor.set_extraction_client(client)

    result = _run(extractor.merge_facts("周武住在杭州", "周武搬到了西湖区"))

    assert result == "周武住在杭州；周武搬到了西湖区"
    assert client.calls == 1


def test_merge_facts_uses_llm_result_when_it_answers_in_time(extractor):
    client = FakeLLMClient(delay=FAST, text="  周武住在杭州西湖区  ")
    extractor.set_extraction_client(client)

    result = _run(extractor.merge_facts("周武住在杭州", "周武搬到了西湖区"))

    assert result == "周武住在杭州西湖区"


def test_merge_facts_timeout_log_names_the_timeout(extractor, caplog):
    """Regression for the blank 'Merge facts error:' line.

    TimeoutError stringifies to '', so the old handler logged a bare colon and
    the operator could not tell a slow provider from a broken response.
    """
    extractor.set_extraction_client(FakeLLMClient(delay=SLOW, text="x"))

    with caplog.at_level(logging.WARNING):
        _run(extractor.merge_facts("a", "b"))

    assert "Merge facts timed out" in caplog.text
    assert "Merge facts error:" not in caplog.text


# ── _check_conflict ──────────────────────────────────────────────────────────

def test_check_conflict_falls_back_to_new_on_timeout(extractor):
    """Timing out must not silently classify a fact as a duplicate."""
    client = FakeLLMClient(delay=SLOW, text="duplicate")
    extractor.set_extraction_client(client)

    assert _run(extractor._check_conflict("新事实", "旧事实")) == "new"


def test_check_conflict_parses_a_quoted_verdict(extractor):
    extractor.set_extraction_client(FakeLLMClient(text='"update"'))

    assert _run(extractor._check_conflict("新事实", "旧事实")) == "update"


def test_check_conflict_rejects_an_unparseable_verdict(extractor):
    """A chatty model that ignores the output contract falls back to 'new'."""
    extractor.set_extraction_client(
        FakeLLMClient(text="I think these two are probably duplicates!")
    )

    assert _run(extractor._check_conflict("新事实", "旧事实")) == "new"


def test_check_conflict_timeout_log_names_the_timeout(extractor, caplog):
    extractor.set_extraction_client(FakeLLMClient(delay=SLOW, text="duplicate"))

    with caplog.at_level(logging.WARNING):
        _run(extractor._check_conflict("a", "b"))

    assert "Conflict check timed out" in caplog.text
    assert "Conflict check error:" not in caplog.text


# ── generate_semantic_id ─────────────────────────────────────────────────────

def test_semantic_id_falls_back_to_empty_on_timeout(extractor):
    """Empty means 'caller derives a hash id' -- never a partial slug."""
    extractor.set_extraction_client(FakeLLMClient(delay=SLOW, text="likes_python"))

    assert _run(extractor.generate_semantic_id("周武喜欢 Python")) == ""


def test_semantic_id_sanitizes_the_model_output(extractor):
    extractor.set_extraction_client(FakeLLMClient(text="Likes Python!!"))

    assert _run(extractor.generate_semantic_id("周武喜欢 Python")) == "likes_python"


# ── no client wired ──────────────────────────────────────────────────────────

def test_every_llm_path_degrades_when_no_client_is_wired(extractor):
    """Startup window: chunks arrive before the host injects an LLM client."""
    assert _run(extractor.extract_personal_facts("对话")) == []
    assert _run(extractor.extract_group_facts("对话")) == []
    assert _run(extractor.extract_facts("对话")) == []
    assert _run(extractor.generate_reflections("qq:1", "user")) == []
    assert _run(extractor.generate_semantic_id("内容")) == ""
    assert _run(extractor._check_conflict("新", "旧")) == "new"
    assert _run(extractor.merge_facts("旧文本", "新文本")) == "旧文本；新文本"
