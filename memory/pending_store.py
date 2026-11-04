"""Persist the in-memory hippocampus pending queue.

KiraOS keeps pre-hippocampus chunks in MemoryManager._pending_conversations.
Losing that state on restart drops up to hippocampus_threshold turns per
session. QanMoCore persisted its topic/batch cache for exactly this reason, so
this store mirrors the idea: a small hidden file under the memory root that is
loaded on startup and rewritten whenever the queue changes.

The file deliberately has no .json suffix: MemoryIndex.rebuild_index_from_files
treats every JSON file under the memory root except profile.json/chat_memory as
a legacy memory, and this queue is runtime state, not a memory.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Dict, List

from core.logging_manager import get_logger

logger = get_logger("kiraos_pending_store", "cyan")

PENDING_FILE_NAME = ".hippocampus_pending"


class PendingStore:
    """Atomic JSON store for a session-to-chunks mapping."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self) -> Dict[str, List[list]]:
        if not self.path.exists():
            return {}
        try:
            raw = self.path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            logger.warning("Cannot read hippocampus pending file %s: %s", self.path, exc)
            return {}
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except ValueError as exc:
            backup = self.path.with_name(self.path.name + f".corrupt_{int(time.time())}")
            try:
                self.path.replace(backup)
            except OSError:
                pass
            logger.warning(
                "Corrupt hippocampus pending file %s (%s); moved to %s and starting empty",
                self.path,
                exc,
                backup,
            )
            return {}
        if not isinstance(data, dict):
            logger.warning("Unrecognised hippocampus pending file %s; starting empty", self.path)
            return {}
        pending: Dict[str, List[list]] = {}
        for session, chunks in data.items():
            if not isinstance(session, str) or not session:
                continue
            if not isinstance(chunks, list) or not chunks:
                continue
            clean = [chunk for chunk in chunks if isinstance(chunk, list)]
            if clean:
                pending[session] = clean
        return pending

    def save(self, pending: Dict[str, List[list]]) -> None:
        clean: Dict[str, List[list]] = {
            str(session): chunks
            for session, chunks in pending.items()
            if session and chunks
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            with tmp.open("w", encoding="utf-8") as handle:
                json.dump(clean, handle, ensure_ascii=False, default=str)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
        except Exception as exc:
            logger.warning("Failed to persist hippocampus pending queue: %s", exc)
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass
