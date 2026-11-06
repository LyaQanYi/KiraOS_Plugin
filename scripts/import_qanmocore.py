#!/usr/bin/env python3
"""CLI wrapper for the MaiBot -> KiraOS memory importer.

Run it with the KiraAI virtualenv:

    python KiraOS_Plugin/scripts/import_qanmocore.py \
        --source ../MaiBot/data \
        --data-dir ../KiraAI/data \
        --dry-run

Only stdlib modules are imported at top level: KiraAI's plugin smoke test
imports every plugin module against host stubs, so the argparse setup must not
require a live KiraAI host. The runtime imports (core.utils.path_utils and the
plugin's memory modules) happen inside main().
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

PLUGIN_ROOT = Path(__file__).resolve().parents[1]


# ── Path discovery ──────────────────────────────────────────────────────────


def _find_source() -> Optional[Path]:
    candidates = [
        PLUGIN_ROOT.parent / "MaiBot" / "data",
        Path.cwd() / "MaiBot" / "data",
        Path.cwd().parent / "MaiBot" / "data",
    ]
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return None


def _default_data_dir() -> Path:
    # Installed layout: <KiraAI>/data/plugins/KiraOS_Plugin
    installed = PLUGIN_ROOT.parent.parent
    if PLUGIN_ROOT.parent.name == "plugins" and installed.name == "data":
        return installed
    # Workspace layout: <workspace>/KiraOS_Plugin and <workspace>/KiraAI
    sibling = PLUGIN_ROOT.parent / "KiraAI" / "data"
    if (PLUGIN_ROOT.parent / "KiraAI").is_dir():
        return sibling
    return PLUGIN_ROOT.parent / "data"


def _find_kira_root(data_dir: Path) -> Optional[Path]:
    for candidate in (data_dir.parent, data_dir.parent.parent):
        if (candidate / "core" / "utils" / "path_utils.py").is_file():
            return candidate
    sibling = PLUGIN_ROOT.parent / "KiraAI"
    if (sibling / "core" / "utils" / "path_utils.py").is_file():
        return sibling
    return None


def _detect_adapter_name(data_dir: Path) -> Optional[str]:
    config_path = data_dir / "config" / "system_config.json"
    if not config_path.is_file():
        return None
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(config, dict):
        return None
    adapters = config.get("adapters")
    if not isinstance(adapters, dict):
        return None
    names = set()
    for adapter_id, entry in adapters.items():
        if not isinstance(entry, dict):
            continue
        if str(entry.get("platform") or "").strip().lower() != "qq":
            continue
        names.add(str(entry.get("name") or adapter_id))
    if len(names) == 1:
        return names.pop()
    return None


def _parse_time(value: str) -> float:
    text = (value or "").strip()
    if not text:
        raise argparse.ArgumentTypeError("empty time value")
    try:
        if len(text) == 10:
            parsed = datetime.strptime(text, "%Y-%m-%d")
        else:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid date/time: {value}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed.timestamp()


# ── Importer bootstrap ──────────────────────────────────────────────────────


def _load_importer(kira_root: Path, data_dir: Path):
    """Import the plugin package under a synthetic name and return modules.

    init_paths() must run before the plugin modules are imported: module-level
    get_logger() calls create <data>/log.log immediately and would otherwise
    point at the process working directory.
    """
    import importlib
    import importlib.util

    sys.path.insert(0, str(kira_root))
    from core.utils.path_utils import init_paths  # noqa: WPS433

    init_paths(data_dir=str(data_dir))
    package_name = "kiraos_plugin_cli"
    spec = importlib.util.spec_from_file_location(
        package_name,
        PLUGIN_ROOT / "__init__.py",
        submodule_search_locations=[str(PLUGIN_ROOT)],
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load the KiraOS plugin package")
    module = importlib.util.module_from_spec(spec)
    sys.modules[package_name] = module
    spec.loader.exec_module(module)

    migration = importlib.import_module(package_name + ".memory.migration_qanmocore")
    paths = importlib.import_module(package_name + ".memory.memory_paths")
    index_mod = importlib.import_module(package_name + ".memory.memory_index")
    store_mod = importlib.import_module(package_name + ".memory.toml_tree_store")
    profile_mod = importlib.import_module(package_name + ".memory.entity_profile")
    return init_paths, migration, paths, index_mod, store_mod, profile_mod


def _lock_is_stale(path: Path) -> bool:
    """Best-effort stale-lock check for a killed import process."""
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
        started = re.search(r"started=([0-9.]+)", content)
        if started:
            try:
                if time.time() - float(started.group(1)) > 24 * 3600:
                    return True
            except ValueError:
                pass
        pid_match = re.search(r"pid=([0-9]+)", content)
        if pid_match:
            pid = int(pid_match.group(1))
            try:
                os.kill(pid, 0)
                return False
            except ProcessLookupError:
                return True
            except PermissionError:
                return False
    except OSError:
        pass
    try:
        return time.time() - path.stat().st_mtime > 24 * 3600
    except OSError:
        return False


class _ImportLock:
    """Exclusive lock so two CLI runs cannot write the same memory tree."""

    def __init__(self, path: Path):
        self.path = path
        self.fd: Optional[int] = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for attempt in range(2):
            try:
                self.fd = os.open(
                    str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644
                )
            except FileExistsError as exc:
                if attempt == 0 and _lock_is_stale(self.path):
                    print(
                        f"WARNING: removing stale import lock {self.path}",
                        file=sys.stderr,
                    )
                    try:
                        self.path.unlink()
                    except OSError:
                        pass
                    continue
                raise RuntimeError(
                    f"another MaiBot import appears to be running (lock: {self.path})"
                ) from exc
            break
        if self.fd is None:
            raise RuntimeError(f"cannot create import lock {self.path}")
        os.write(self.fd, f"pid={os.getpid()} started={time.time()}".encode("utf-8"))
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None
        try:
            self.path.unlink()
        except OSError:
            pass


def _backup_memory_root(memory_root: Path, data_dir: Path) -> Optional[Path]:
    if not memory_root.is_dir():
        return None
    has_data = any(memory_root.rglob("*.toml"))
    if not has_data and not any(memory_root.rglob("profile.json")):
        return None
    stamp = time.strftime("%Y%m%d-%H%M%S")
    destination = data_dir / "migration_backups" / f"memory-{stamp}"
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(memory_root, destination)
    return destination


def _check_target_idle(memory_root: Path) -> None:
    target_wal = memory_root / "memory_index.db-wal"
    if target_wal.exists() and target_wal.stat().st_size > 0:
        raise RuntimeError(
            "data/memory/memory_index.db-wal is non-empty; KiraAI may be "
            "running. Stop it first (or pass --allow-running)."
        )


def _write_report(
    data_dir: Path, stats, options, manifest_path, path: Optional[Path] = None,
) -> Path:
    if path is None:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        path = data_dir / "migration_reports" / f"qanmocore-{stamp}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now().astimezone().isoformat(),
        "manifest": str(manifest_path),
        "options": {
            "adapter": options.adapter,
            "include": sorted(options.include),
            "profile_facts_limit": options.profile_facts_limit,
            "since": options.since,
            "until": options.until,
            "max_per_chat": options.max_per_chat,
            "keep_unanswered": options.keep_unanswered,
            "dry_run": options.dry_run,
        },
        "stats": stats.to_dict(),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _print_summary(stats, source_dir: Path, report_path: Optional[Path]) -> None:
    print("=" * 64)
    print("MaiBot -> KiraAI / KiraOS memory import")
    print(f"  source: {source_dir}")
    print(f"  target: {stats.output_root}")
    print(f"  dry-run: {stats.dry_run}")
    print("-" * 64)
    print(f"  imported: {stats.total_imported}")
    for category, count in sorted(stats.imported.items()):
        print(f"    + {category}: {count}")
    if stats.updated:
        print(f"  updated: {stats.total_updated}")
        for category, count in sorted(stats.updated.items()):
            print(f"    ~ {category}: {count}")
    print(f"  profiles written: {stats.profiles_written}")
    if stats.skipped:
        print("  skipped:")
        for reason, count in sorted(stats.skipped.items()):
            print(f"    - {reason}: {count}")
    if stats.errors:
        print(f"  errors: {len(stats.errors)}")
        for item in stats.errors[:10]:
            print(f"    ! {item}")
    print(f"  elapsed: {stats.elapsed:.1f}s")
    if report_path is not None:
        print(f"  report: {report_path}")
    print("=" * 64)


# ── Entry point ─────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Import MaiBot long-term memory into KiraOS TOML storage."
    )
    parser.add_argument("--source", type=Path, default=None,
                        help="MaiBot data directory (contains MaiBot.db)")
    parser.add_argument("--data-dir", type=Path, default=None,
                        help="KiraAI data directory (defaults to the installed plugin layout)")
    parser.add_argument("--kira-root", type=Path, default=None,
                        help="KiraAI repository root (defaults to the parent of data-dir)")
    parser.add_argument("--adapter", type=str, default=None,
                        help="Entity id prefix; auto-detected from the QQ adapter, else qq")
    parser.add_argument("--include", type=str, default=None,
                        help="Comma-separated categories (default: profiles,history,chest,jargon,qa,conflicts,memory)")
    parser.add_argument("--exclude", type=str, default="",
                        help="Comma-separated categories to drop")
    parser.add_argument("--profile-facts", type=int, default=12,
                        help="Maximum facts copied into each profile.json (default 12)")
    parser.add_argument("--profile-facts-max-chars", type=int, default=1200,
                        help="Profile facts character cap (default 1200)")
    parser.add_argument("--since", type=_parse_time, default=None,
                        help="Only import source rows at/after this date (YYYY-MM-DD)")
    parser.add_argument("--until", type=_parse_time, default=None,
                        help="Only import source rows at/before this date (YYYY-MM-DD)")
    parser.add_argument("--max-per-chat", type=int, default=None,
                        help="Cap history/QA rows per chat stream (newest first)")
    parser.add_argument("--keep-unanswered", action="store_true",
                        help="Keep 'cannot confirm / not found' QA answers")
    parser.add_argument("--include-hippo-topics", action="store_true",
                        help="Also import hippo_memorizer topic batches")
    parser.add_argument("--dry-run", action="store_true",
                        help="Count what would be imported; write nothing to the target")
    parser.add_argument("--force", action="store_true",
                        help="Re-import memories recorded as deleted by a previous run")
    parser.add_argument("--allow-live-source", action="store_true",
                        help="Read even if MaiBot.db-wal is non-empty (old bot may be running)")
    parser.add_argument("--allow-running", action="store_true",
                        help="Proceed even if the target index looks like it is in use")
    parser.add_argument("--no-backup", action="store_true",
                        help="Do not copy an existing memory tree before writing")
    parser.add_argument("--rebuild-index", action="store_true",
                        help="Rebuild memory_index.db after the import (KiraAI does it on start)")
    parser.add_argument("--report", type=Path, default=None,
                        help="Write the JSON report to this path")
    parser.add_argument("--fail-on-error", action="store_true",
                        help="Exit non-zero if any individual item failed")
    parser.add_argument("--verbose", action="store_true",
                        help="Enable debug logging")
    return parser


def main(argv: Optional[list] = None) -> int:
    args = build_parser().parse_args(argv)

    data_dir = (args.data_dir or _default_data_dir()).resolve()

    kira_root = (args.kira_root or _find_kira_root(data_dir))
    if kira_root is None:
        print(
            "ERROR: cannot locate the KiraAI checkout. Pass --kira-root /path/to/KiraAI.",
            file=sys.stderr,
        )
        return 2

    source_dir = (args.source or _find_source())
    if source_dir is None or not (source_dir / "MaiBot.db").is_file():
        print(
            "ERROR: cannot locate MaiBot/data/MaiBot.db. Pass --source /path/to/MaiBot/data.",
            file=sys.stderr,
        )
        return 2
    source_dir = source_dir.resolve()

    memory_root = data_dir / "memory"
    if not args.dry_run and not args.allow_running:
        try:
            # Bootstrap creates a log file, so guard the target before loading it.
            _check_target_idle(memory_root)
        except RuntimeError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 3

    try:
        init_paths, migration, paths, index_mod, store_mod, profile_mod = _load_importer(
            kira_root.resolve(), data_dir
        )
    except Exception as exc:
        print(f"ERROR: cannot import KiraAI/plugin modules: {exc}", file=sys.stderr)
        print(f"       (KiraAI root: {kira_root}; is its virtualenv active?)", file=sys.stderr)
        return 2

    init_paths(data_dir=str(data_dir))
    paths.set_data_root(memory_root)

    adapter = args.adapter or _detect_adapter_name(data_dir) or "qq"

    include = set(migration.DEFAULT_INCLUDE)
    if args.include is not None:
        include = migration._normalize_categories([args.include])
    if args.exclude:
        include -= migration._normalize_categories([args.exclude])
    if args.include_hippo_topics:
        include.add(migration.CATEGORY_HIPPO)

    options = migration.ImportOptions(
        adapter=adapter,
        include=include,
        profile_facts_limit=args.profile_facts,
        profile_facts_max_chars=args.profile_facts_max_chars,
        since=args.since,
        until=args.until,
        max_per_chat=args.max_per_chat,
        keep_unanswered=args.keep_unanswered,
        force=args.force,
        dry_run=args.dry_run,
        manifest_path=memory_root / migration.MANIFEST_NAME,
    )

    import asyncio

    if args.dry_run:
        try:
            stats = asyncio.run(
                migration.import_qanmocore(
                    source_dir,
                    options=options,
                    allow_live_source=args.allow_live_source,
                )
            )
        except RuntimeError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 3
        _print_summary(stats, source_dir, None)
        if args.fail_on_error and stats.errors:
            return 4
        return 0

    index = None
    store = None
    backup_path = None
    try:
        with _ImportLock(memory_root / ".qanmocore_import.lock"):
            if not args.allow_running:
                _check_target_idle(memory_root)
            migration.load_manifest(options.manifest_path)
            if not args.no_backup:
                backup_path = _backup_memory_root(memory_root, data_dir)
                if backup_path is not None:
                    print(f"Backed up existing memory tree to {backup_path}")

            index = index_mod.MemoryIndex()
            store = store_mod.TomlTreeStore(index=index)
            profile_store = profile_mod.EntityProfileStore()
            stats = asyncio.run(
                migration.import_qanmocore(
                    source_dir,
                    tree_store=store,
                    profile_store=profile_store,
                    options=options,
                    allow_live_source=args.allow_live_source,
                )
            )
            if args.rebuild_index:
                asyncio.run(store.rebuild_index())
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 3
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    finally:
        if store is not None:
            try:
                store.close()
            except Exception:
                pass

    report_path = None
    try:
        report_path = _write_report(
            data_dir, stats, options, memory_root / migration.MANIFEST_NAME, args.report
        )
    except Exception as exc:
        print(f"WARNING: could not write report: {exc}", file=sys.stderr)

    _print_summary(stats, source_dir, report_path)
    if args.fail_on_error and stats.errors:
        return 4
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
