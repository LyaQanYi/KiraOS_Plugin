"""Import smoke tests.

These exist to gate the host-stub surface in `conftest.py`. Every module the
plugin ships must import cleanly against the stubs; when a hand-merge (PR 2)
or a feature port (PR 5) pulls in a new `from core...` import, this fails
immediately with the missing name rather than at plugin-load time inside
KiraAI — where a raised `initialize()` leaves zero tools registered and the
failure looks like "memory just stopped working".
"""

from __future__ import annotations

import importlib
import pkgutil
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def _plugin_modules() -> list[str]:
    """Every module under the plugin, as `plugins.KiraOS_Plugin.*`."""
    names: list[str] = []
    for path in sorted(REPO_ROOT.rglob("*.py")):
        rel = path.relative_to(REPO_ROOT)
        if rel.parts[0] in {"tests", ".git", "vendor"}:
            continue
        parts = list(rel.parts)
        parts[-1] = parts[-1][:-3]  # strip .py
        if parts[-1] == "__init__":
            parts.pop()
        if not parts:
            continue
        names.append("plugins.KiraOS_Plugin." + ".".join(parts))
    return names


def test_plugin_module_list_is_not_empty():
    """Guard the guard — a bad rglob would make every other test vacuous."""
    mods = _plugin_modules()
    assert mods, "no plugin modules discovered; the walker is broken"
    assert "plugins.KiraOS_Plugin.main" in mods


@pytest.mark.parametrize("module_name", _plugin_modules())
def test_module_imports_against_host_stubs(module_name):
    importlib.import_module(module_name)


def test_memory_package_exports():
    """The public surface `main.py` and the outer layer rely on."""
    mem = importlib.import_module("plugins.KiraOS_Plugin.memory")
    assert hasattr(mem, "MemoryManager")


def test_plugin_class_constructs_without_host():
    """`__init__` must not touch the network, the disk, or a live host.

    It reads config and computes paths only; everything with a side effect
    belongs in `initialize()`. This keeps plugin construction safe during
    KiraAI's registration pass.
    """
    main = importlib.import_module("plugins.KiraOS_Plugin.main")
    plugin_cls = getattr(main, "UserMemoryPlugin")
    instance = plugin_cls(ctx=None, cfg={})
    assert instance is not None
