"""Pytest bootstrap for the KiraOS plugin test suite.

The plugin imports the KiraAI host (`core.plugin`, `core.provider`,
`core.chat.*`, ...), which is not installable as a package and whose provider
stack has a circular import that only resolves during a real `KiraLifecycle`
boot. Rather than requiring a KiraAI checkout next to this repo, we install
lightweight stand-ins for exactly the host surface the plugin touches, so the
suite is hermetic and CI needs nothing but this repository.

The stubs are installed at conftest import time — before pytest collects any
test module — and are deliberately unconditional, so a developer running the
suite from inside a KiraAI tree gets the same deterministic behaviour as CI.
End-to-end verification against the real host is a manual step (see README).

Host surface covered here is the union of what both merged codebases import:

    core.logging_manager        get_logger
    core.plugin                 BasePlugin, logger, on, Priority,
                                register, register_tool
    core.prompt_manager         Prompt
    core.provider               LLMRequest, LLMResponse, LLMModelClient
    core.utils.path_utils       get_data_path, get_config_path, get_root_path
    core.chat.message_elements  Text, BaseMessageElement
    core.chat.message_utils     KiraMessageEvent, KiraMessageBatchEvent,
                                KiraStepResult
"""

from __future__ import annotations

import logging
import sys
import types
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# ── The data root the stubbed host hands back ────────────────────────────────
# Tests that need a real directory override this via the `data_root` fixture
# below; the default keeps `get_data_path()` from ever pointing at a live
# install if some import-time code calls it.
_STUB_DATA_ROOT: Optional[Path] = None


def _set_stub_data_root(path) -> None:
    global _STUB_DATA_ROOT
    _STUB_DATA_ROOT = Path(path) if path is not None else None


def _module(name: str) -> types.ModuleType:
    """Create-or-fetch a module in sys.modules, wiring it onto its parent.

    `from a.b.c import x` makes Python import `a`, then `a.b`, then `a.b.c`,
    and it resolves each level as an attribute of the previous one — so a bare
    sys.modules entry for the leaf is not enough.
    """
    if name in sys.modules:
        return sys.modules[name]
    mod = types.ModuleType(name)
    mod.__path__ = []  # mark as a package so submodules can hang off it
    sys.modules[name] = mod
    if "." in name:
        parent_name, _, leaf = name.rpartition(".")
        parent = _module(parent_name)
        setattr(parent, leaf, mod)
    return mod


def _install_host_stubs() -> None:
    # ── core.logging_manager ─────────────────────────────────────────────
    lm = _module("core.logging_manager")

    def get_logger(name: str, color: str = ""):
        # The real one attaches a RotatingFileHandler writing to data/log.log,
        # which would need a live data dir. A plain logger is enough here.
        return logging.getLogger(name)

    lm.get_logger = get_logger

    # ── core.provider ────────────────────────────────────────────────────
    provider = _module("core.provider")

    @dataclass
    class LLMRequest:
        messages: list = field(default_factory=list)
        system_prompt: list = field(default_factory=list)
        user_prompt: list = field(default_factory=list)

    @dataclass
    class LLMResponse:
        text_response: str = ""

    class LLMModelClient:
        pass

    provider.LLMRequest = LLMRequest
    provider.LLMResponse = LLMResponse
    provider.LLMModelClient = LLMModelClient

    # ── core.prompt_manager ──────────────────────────────────────────────
    pm = _module("core.prompt_manager")

    class Prompt:
        # Mirrors core/prompt_manager.py:12-20. `persist` matters: the recall
        # query builder skips non-persistent prompts, so a stub that dropped it
        # would silently change recall behaviour under test.
        def __init__(self, content: str, name=None, source=None,
                     end="\n", persist: bool = True, **kwargs):
            self.content = content
            self.name = name
            self.source = source
            self.end = end
            self.persist = persist
            self.kwargs = kwargs

        def __repr__(self):
            return f"Prompt(name={self.name}, source={self.source})"

    pm.Prompt = Prompt

    # ── core.utils.path_utils ────────────────────────────────────────────
    pu = _module("core.utils.path_utils")

    def get_root_path() -> Path:
        return Path.cwd().resolve()

    def get_data_path() -> Path:
        if _STUB_DATA_ROOT is not None:
            return _STUB_DATA_ROOT
        return get_root_path() / "data"

    def get_config_path() -> Path:
        return get_data_path() / "config"

    pu.get_root_path = get_root_path
    pu.get_data_path = get_data_path
    pu.get_config_path = get_config_path

    # ── core.chat.message_elements ───────────────────────────────────────
    me = _module("core.chat.message_elements")

    class BaseMessageElement:
        pass

    class Text(BaseMessageElement):
        def __init__(self, text: str):
            self.text = text

        @property
        def repr(self) -> str:
            return self.text

    me.BaseMessageElement = BaseMessageElement
    me.Text = Text

    # ── core.chat.message_utils ──────────────────────────────────────────
    mu = _module("core.chat.message_utils")

    @dataclass
    class KiraStepResult:
        raw_output: str = ""

    @dataclass
    class KiraMessageEvent:
        message: object = None
        adapter: object = None
        session: object = None
        sid: str = ""

    @dataclass
    class KiraMessageBatchEvent:
        messages: list = field(default_factory=list)
        adapter: object = None
        session: object = None
        sid: str = ""

    mu.KiraStepResult = KiraStepResult
    mu.KiraMessageEvent = KiraMessageEvent
    mu.KiraMessageBatchEvent = KiraMessageBatchEvent

    # ── core.plugin ──────────────────────────────────────────────────────
    plugin = _module("core.plugin")

    class BasePlugin:
        def __init__(self, ctx=None, cfg=None):
            self.ctx = ctx
            self.plugin_cfg = cfg or {}

    class _Decorators:
        """Stands in for `on` / `register` — every attribute is a no-op
        decorator factory, so `@on.llm_request(priority=...)` and
        `@register.tool(name=..., params=...)` both just return the function
        untouched."""

        def __getattr__(self, _name):
            def _factory(*_args, **_kwargs):
                # Support both `@on.foo` and `@on.foo(...)`.
                if len(_args) == 1 and callable(_args[0]) and not _kwargs:
                    return _args[0]
                return lambda fn: fn
            return _factory

    class Priority:
        HIGHEST = 0
        HIGH = 1
        MEDIUM = 2
        LOW = 3
        LOWEST = 4

    plugin.BasePlugin = BasePlugin
    plugin.logger = logging.getLogger("kiraos.test")
    plugin.on = _Decorators()
    plugin.register = _Decorators()
    plugin.register_tool = _Decorators().tool
    plugin.Priority = Priority

    # ── Make the plugin importable as `plugins.KiraOS_Plugin.*` ──────────
    # The plugin's own modules use relative imports, but the tests address it
    # by its installed dotted path. __path__ is derived from this file, so the
    # checkout directory may be named anything.
    repo_root = Path(__file__).resolve().parents[1]
    if "plugins" not in sys.modules:
        pkg = types.ModuleType("plugins")
        pkg.__path__ = [str(repo_root.parent)]
        sys.modules["plugins"] = pkg
    if "plugins.KiraOS_Plugin" not in sys.modules:
        sub = types.ModuleType("plugins.KiraOS_Plugin")
        sub.__path__ = [str(repo_root)]
        sys.modules["plugins.KiraOS_Plugin"] = sub
        setattr(sys.modules["plugins"], "KiraOS_Plugin", sub)


_install_host_stubs()


# ── Fixtures ─────────────────────────────────────────────────────────────────

import pytest  # noqa: E402  (must follow stub installation)


@pytest.fixture
def data_root(tmp_path, monkeypatch):
    """A throwaway data root, with the stubbed `get_data_path()` pointed at it.

    Yields the path so a test can lay out `memory/`, `plugin_data/`, etc. under
    it exactly as a live install would.
    """
    root = tmp_path / "data"
    root.mkdir(parents=True, exist_ok=True)
    _set_stub_data_root(root)
    try:
        yield root
    finally:
        _set_stub_data_root(None)
