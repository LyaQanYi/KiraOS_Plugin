"""Per-session cache of the most recent successful recall block.

MaiBot's thinking_back table reused answers found in the last ten minutes so
the next reply could still refer to a fact the model had just "remembered".
KiraOS recomputes recall from scratch every turn; when the newest message no
longer matches the earlier query, that context disappears. This cache keeps one
short recall block per session for a TTL and lets inject_context append it as a
separate [最近回忆] scope.

It is runtime state, not a memory: the file has no .json suffix so the startup
index rebuild does not scan it as a legacy memory.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Dict, Optional

from core.logging_manager import get_logger

logger = get_logger("kiraos_recall_cache", "cyan")

RECALL_CACHE_FILE_NAME = ".recall_cache"
MAX_ENTRIES = 200


class RecallCache:
    """TTL cache of the last successful recall text per session."""

    def __init__(self, path: Path, ttl: float = 600.0, max_entries: int = MAX_ENTRIES):
        self.path = Path(path)
        self.ttl = max(0.0, float(ttl))
        self.max_entries = max(1, int(max_entries))
        self._entries: Dict[str, dict] = self._load()

    def _load(self) -> Dict[str, dict]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("Cannot read recall cache %s: %s", self.path, exc)
            return {}
        if not isinstance(data, dict):
            return {}
        now = time.time()
        entries: Dict[str, dict] = {}
        for session, entry in data.items():
            if not isinstance(session, str) or not isinstance(entry, dict):
                continue
            ts = entry.get("ts")
            text = entry.get("text")
            if not isinstance(ts, (int, float)) or not isinstance(text, str) or not text.strip():
                continue
            if self.ttl <= 0 or now - float(ts) > self.ttl:
                continue
            entries[session] = {
                "query": str(entry.get("query") or ""),
                "text": text,
                "ts": float(ts),
            }
        return entries

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            with tmp.open("w", encoding="utf-8") as handle:
                json.dump(self._entries, handle, ensure_ascii=False, default=str)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
        except Exception as exc:
            logger.warning("Failed to persist recall cache: %s", exc)
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass

    def _prune(self, now: float) -> None:
        if self.ttl > 0:
            self._entries = {
                session: entry
                for session, entry in self._entries.items()
                if now - float(entry.get("ts") or 0) <= self.ttl
            }
        if len(self._entries) > self.max_entries:
            ordered = sorted(
                self._entries.items(), key=lambda item: float(item[1].get("ts") or 0), reverse=True
            )
            self._entries = dict(ordered[: self.max_entries])

    def record(self, session_id: str, query: str, text: str, timestamp: Optional[float] = None) -> None:
        if self.ttl <= 0 or not session_id or not text or not text.strip():
            return
        now = float(timestamp or time.time())
        self._entries[session_id] = {
            "query": str(query or ""),
            "text": text.strip(),
            "ts": now,
        }
        self._prune(now)
        self._save()

    def get(self, session_id: str, timestamp: Optional[float] = None) -> Optional[dict]:
        if self.ttl <= 0 or not session_id:
            return None
        entry = self._entries.get(session_id)
        if not entry:
            return None
        now = float(timestamp or time.time())
        if now - float(entry.get("ts") or 0) > self.ttl:
            self._entries.pop(session_id, None)
            return None
        return dict(entry)

    def clear(self, session_id: Optional[str] = None) -> None:
        if session_id is None:
            self._entries = {}
        else:
            self._entries.pop(session_id, None)
        self._save()

    def __len__(self) -> int:
        return len(self._entries)
