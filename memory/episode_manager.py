"""Episode (event) memory.

MaiBot's most distinctive memory layer was chat_history: it segmented a long
conversation into topics, summarized each topic into theme/summary/keywords/
key_points, and later let a ReAct agent search those event summaries. KiraOS's
hippocampus extracts atomic facts and reflections, but loses the narrative of
what happened in a session.

EpisodeManager records a rolling window of every message in a session (user and
assistant, whether or not the bot was triggered), and when the window is large
enough or old enough asks the extraction LLM for one compact event summary. The
summary is written as a normal KiraOS memory:

    entities/{group|user}_{id}/episodes/episode_*.toml
    type = "episode", importance = 6, tags = keywords, source = participants

Episodes participate in the same FTS index and recall path as facts and
reflections, so the plugin's existing memory_search and injection can use them.

Buffers are persisted to a hidden .episode_buffers file (no .json suffix so the
startup index rebuild does not treat it as a legacy memory).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from core.logging_manager import get_logger

from .identity import resolve_entity_id
from .llm_adapter import chat_text
from .memory_index import MemoryIndex, normalize_tags
from .memory_paths import ENTITY_GROUP, ENTITY_USER, get_memory_root
from .toml_tree_store import TomlTreeStore

logger = get_logger("kiraos_episode", "cyan")

EPISODE_BUFFER_FILE_NAME = ".episode_buffers"
_MIN_MESSAGES = 8
_MAX_BUFFER_MESSAGES = 200
_MAX_CONVERSATION_CHARS = 8000
_MAX_MESSAGE_CHARS = 600

_EPISODE_PROMPT = """你是记忆整理助手。下面是一段{chat_type}的聊天记录。
请把它总结成一条“事件记忆”，保留人物、事件发展过程和结果，不要编造。

要求：
1. theme：一句话主题，包含大致时间/人物/事件；
2. summary：100-300 字的平实概括，可以摘取原文；
3. keywords：3-8 个关键词；
4. participants：参与者昵称列表。

只输出 JSON 对象，不要输出其他内容：
{
  "theme": "一句话主题",
  "summary": "事件概括",
  "keywords": ["关键词1", "关键词2"],
  "participants": ["昵称1", "昵称2"]
}

聊天记录：
{conversation_text}
"""


def _sha8(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]


def _clean_text(value) -> str:
    if value is None:
        return ""
    return str(value).replace("\r\n", "\n").replace("\r", "\n").strip()


class EpisodeManager:
    """Rolling per-session buffer that turns conversation windows into episodes."""

    def __init__(
        self,
        tree_store: TomlTreeStore,
        *,
        buffer_path: Optional[Path] = None,
        threshold: int = 40,
        window_minutes: int = 30,
        require_bot: bool = True,
        flush_interval: int = 60,
        min_messages: int = _MIN_MESSAGES,
        max_buffer_messages: int = _MAX_BUFFER_MESSAGES,
        importance: int = 6,
        llm_timeout: float = 60.0,
    ):
        self.tree_store = tree_store
        self.threshold = max(5, int(threshold))
        self.window_seconds = max(60, int(window_minutes) * 60)
        self.require_bot = bool(require_bot)
        self.flush_interval = max(10, int(flush_interval))
        self.min_messages = max(2, int(min_messages))
        self.max_buffer_messages = max(self.threshold, int(max_buffer_messages))
        self.importance = max(1, min(10, int(importance)))
        self.llm_timeout = max(5.0, float(llm_timeout))
        self.buffer_path = Path(buffer_path) if buffer_path else Path(get_memory_root()) / EPISODE_BUFFER_FILE_NAME
        self._buffers: Dict[str, dict] = self._load_buffers()
        self._llm = None
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._dirty = False
        self._last_save = 0.0
        logger.info(
            "EpisodeManager initialized: threshold=%d window=%dm require_bot=%s buffers=%d",
            self.threshold,
            self.window_seconds // 60,
            self.require_bot,
            len(self._buffers),
        )

    # ── lifecycle ──────────────────────────────────────────────────────

    def set_llm_client(self, client) -> None:
        self._llm = client

    def start(self) -> None:
        if self._running:
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("Episode summarizer loop started (interval=%ds)", self.flush_interval)

    def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            self._task = None
        self._save_buffers()
        logger.info("Episode summarizer stopped")

    async def _loop(self) -> None:
        try:
            while self._running:
                await asyncio.sleep(self.flush_interval)
                await self.flush_due()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("Episode summarizer loop failed: %s", exc, exc_info=True)
            self._running = False

    async def flush_due(self) -> int:
        """Flush windows older than window_seconds; returns number of episodes."""
        now = time.time()
        sessions = [
            session
            for session, buffer in list(self._buffers.items())
            if buffer.get("messages")
            and now - float(buffer.get("start") or now) >= self.window_seconds
        ]
        count = 0
        for session in sessions:
            if await self.flush_session(session):
                count += 1
        if self._dirty:
            self._save_buffers()
        return count

    # ── ingestion ──────────────────────────────────────────────────────

    def record_message(
        self,
        session: str,
        role: str,
        content: str,
        *,
        sender_id: str = "",
        sender_name: str = "",
        timestamp: Optional[float] = None,
    ) -> None:
        if not session or not self._parse_session(session):
            return
        text = _clean_text(content)
        if not text:
            return
        if len(text) > _MAX_MESSAGE_CHARS:
            text = text[: _MAX_MESSAGE_CHARS - 1] + "…"
        now = float(timestamp or time.time())
        buffer = self._buffers.get(session)
        if not buffer:
            buffer = {"messages": [], "start": now, "end": now, "has_bot": False}
            self._buffers[session] = buffer
        buffer["messages"].append(
            {
                "role": "assistant" if role == "assistant" else "user",
                "content": text,
                "sender_id": str(sender_id or ""),
                "sender_name": str(sender_name or ""),
                "time": now,
            }
        )
        buffer["end"] = now
        if role == "assistant":
            buffer["has_bot"] = True
        if len(buffer["messages"]) > self.max_buffer_messages:
            buffer["messages"] = buffer["messages"][-self.max_buffer_messages :]
        self._dirty = True
        if len(buffer["messages"]) >= self.threshold and buffer.get("has_bot"):
            self._schedule_flush(session)
        self._maybe_save()

    def _schedule_flush(self, session: str) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self._flush_safe(session))

    async def _flush_safe(self, session: str) -> None:
        try:
            await self.flush_session(session)
        except Exception as exc:
            logger.warning("Episode flush failed for %s: %s", session, exc)

    # ── summarization ──────────────────────────────────────────────────

    async def flush_session(self, session: str) -> bool:
        buffer = self._buffers.pop(session, None)
        if not buffer:
            return False
        messages = buffer.get("messages") or []
        if len(messages) < self.min_messages:
            self._dirty = True
            self._maybe_save(force=True)
            return False
        if self.require_bot and not buffer.get("has_bot"):
            self._dirty = True
            self._maybe_save(force=True)
            return False
        parsed = self._parse_session(session)
        if parsed is None:
            return False
        entity_id, entity_type = parsed
        if self._llm is None:
            # Keep the window until set_llm_client()/the next loop run.
            self._buffers[session] = buffer
            return False

        conversation_text = self._build_conversation_text(messages)
        try:
            summary = await self._summarize(
                session, entity_type, conversation_text, messages
            )
        except asyncio.CancelledError:
            # Shutdown cancelled the flush mid-LLM-call: put the window back so
            # stop()'s final save persists it for the next start.
            self._buffers[session] = buffer
            self._save_buffers()
            raise
        if summary is None:
            failures = int(buffer.get("failures") or 0) + 1
            if failures >= 3:
                logger.warning(
                    "Episode summary failed %d times for %s; dropping window", failures, session
                )
                self._dirty = True
                self._maybe_save(force=True)
                return False
            buffer["failures"] = failures
            self._buffers[session] = buffer
            self._dirty = True
            self._maybe_save(force=True)
            return False

        text = summary["text"]
        text_hash = MemoryIndex.content_hash(text)
        try:
            existing = await asyncio.to_thread(
                self.tree_store.index.find_by_hash,
                text_hash,
                entity_id=entity_id,
                entity_type=entity_type,
                folder="episodes",
            )
        except Exception:
            existing = None
        if existing:
            logger.debug("Episode duplicate for %s skipped", session)
            self._dirty = True
            self._maybe_save(force=True)
            return True

        start = float(buffer.get("start") or time.time())
        end = float(buffer.get("end") or start)
        tags = normalize_tags(["episode"] + summary["keywords"])
        source = {
            "origin": "kiraos_episode",
            "session": session,
            "time": _iso(end),
            "start_time": _iso(start),
            "end_time": _iso(end),
            "participants": summary["participants"],
            "message_count": len(messages),
        }
        semantic_id = f"episode_{_sha8(session)}_{int(start)}_{text_hash[:8]}"
        try:
            memory = await self.tree_store.add_memory(
                content_text=text,
                memory_type="episode",
                importance=self.importance,
                tags=tags,
                source=source,
                semantic_id=semantic_id,
                entity_id=entity_id,
                entity_type=entity_type,
                folder="episodes",
            )
            memory.meta.update(
                timestamp=start, last_accessed=time.time(), access_count=0
            )
            await self.tree_store.update_memory(memory)
        except Exception as exc:
            logger.warning("Could not store episode for %s: %s", session, exc)
            self._buffers[session] = buffer
            self._dirty = True
            self._maybe_save(force=True)
            return False

        self._dirty = True
        self._maybe_save(force=True)
        logger.info(
            "Episode stored: %s %s messages=%d theme=%s",
            entity_type,
            entity_id,
            len(messages),
            summary.get("theme", "")[:60],
        )
        return True

    async def _summarize(
        self, session: str, entity_type: str, conversation_text: str, messages: List[dict]
    ) -> Optional[dict]:
        chat_type = "群聊" if entity_type == ENTITY_GROUP else "私聊"
        prompt = (
            _EPISODE_PROMPT.replace("{chat_type}", chat_type)
            .replace("{conversation_text}", conversation_text)
        )
        try:
            response = await asyncio.wait_for(
                chat_text(self._llm, prompt), timeout=self.llm_timeout
            )
        except asyncio.TimeoutError:
            logger.warning("Episode summary timed out for %s", session)
            return None
        except Exception as exc:
            logger.warning("Episode summary failed for %s: %s", session, exc)
            return None
        response = _clean_text(response)
        if not response:
            return None
        parsed = _parse_summary_json(response)
        theme = parsed.get("theme") or ""
        summary_text = parsed.get("summary") or response[:1000]
        summary_text = _clean_text(summary_text)
        if not summary_text:
            return None
        keywords = [str(k).strip() for k in parsed.get("keywords") or [] if str(k).strip()]
        participants = [str(p).strip() for p in parsed.get("participants") or [] if str(p).strip()]
        if not participants:
            participants = _participants_from_messages(messages)
        text = f"{theme}\n{summary_text}" if theme and theme not in summary_text else summary_text
        return {
            "theme": theme,
            "text": text,
            "keywords": keywords[:8],
            "participants": participants[:20],
        }

    # ── helpers ────────────────────────────────────────────────────────

    @staticmethod
    def _parse_session(session: str) -> Optional[Tuple[str, str]]:
        parts = str(session or "").split(":", maxsplit=2)
        if len(parts) != 3:
            return None
        adapter, session_type, session_id = parts
        if not adapter or not session_id:
            return None
        if session_type == "gm":
            return f"{adapter}:{session_id}", ENTITY_GROUP
        if session_type == "dm":
            return resolve_entity_id(f"{adapter}:{session_id}", ENTITY_USER), ENTITY_USER
        return None

    @staticmethod
    def _build_conversation_text(messages: List[dict]) -> str:
        lines = []
        total = 0
        omitted = False
        for message in reversed(messages):
            stamp = time.strftime("%m-%d %H:%M", time.localtime(float(message.get("time") or 0)))
            role = "bot" if message.get("role") == "assistant" else (message.get("sender_name") or "用户")
            line = f"{stamp} {role}: {message.get('content') or ''}"
            if total + len(line) > _MAX_CONVERSATION_CHARS:
                omitted = True
                break
            lines.append(line)
            total += len(line) + 1
        lines.reverse()
        if omitted:
            lines.insert(0, "…（更早消息已省略）")
        return "\n".join(lines)

    def _buffers_snapshot(self) -> Dict[str, dict]:
        return self._buffers

    def _load_buffers(self) -> Dict[str, dict]:
        path = self.buffer_path
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("Cannot read episode buffers %s: %s", path, exc)
            return {}
        if not isinstance(data, dict):
            return {}
        buffers: Dict[str, dict] = {}
        now = time.time()
        for session, buffer in data.items():
            if not isinstance(session, str) or not isinstance(buffer, dict):
                continue
            messages = buffer.get("messages")
            if not isinstance(messages, list) or not messages:
                continue
            clean = []
            for message in messages:
                if not isinstance(message, dict):
                    continue
                content = _clean_text(message.get("content"))
                if not content:
                    continue
                clean.append(
                    {
                        "role": "assistant" if message.get("role") == "assistant" else "user",
                        "content": content,
                        "sender_id": str(message.get("sender_id") or ""),
                        "sender_name": str(message.get("sender_name") or ""),
                        "time": float(message.get("time") or now),
                    }
                )
            if not clean:
                continue
            buffers[session] = {
                "messages": clean[-self.max_buffer_messages :],
                "start": float(buffer.get("start") or clean[0]["time"]),
                "end": float(buffer.get("end") or clean[-1]["time"]),
                "has_bot": bool(buffer.get("has_bot")),
            }
        return buffers

    def _save_buffers(self) -> None:
        self.buffer_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.buffer_path.with_name(self.buffer_path.name + ".tmp")
        try:
            with tmp.open("w", encoding="utf-8") as handle:
                json.dump(self._buffers, handle, ensure_ascii=False, default=str)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.buffer_path)
            self._dirty = False
            self._last_save = time.time()
        except Exception as exc:
            logger.warning("Failed to persist episode buffers: %s", exc)
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass

    def _maybe_save(self, *, force: bool = False) -> None:
        now = time.time()
        if force or (self._dirty and now - self._last_save >= 5.0):
            self._save_buffers()


def _iso(timestamp: float) -> str:
    from datetime import datetime, timezone

    try:
        return datetime.fromtimestamp(float(timestamp), tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return datetime.now(tz=timezone.utc).isoformat()


def _parse_summary_json(response: str) -> dict:
    text = (response or "").strip()
    fence = chr(96) * 3
    if text.startswith(fence):
        text = re.sub(r"^" + fence + r"(?:json)?\s*", "", text)
        text = re.sub(r"\s*" + fence + r"$", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        candidate = text[start : end + 1]
    else:
        candidate = text
    try:
        data = json.loads(candidate)
    except (TypeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    keywords = data.get("keywords")
    participants = data.get("participants")
    if isinstance(keywords, str):
        keywords = [keywords]
    if isinstance(participants, str):
        participants = [participants]
    return {
        "theme": _clean_text(data.get("theme")),
        "summary": _clean_text(data.get("summary")),
        "keywords": keywords if isinstance(keywords, list) else [],
        "participants": participants if isinstance(participants, list) else [],
    }


def _participants_from_messages(messages: List[dict]) -> List[str]:
    out: List[str] = []
    for message in messages:
        name = _clean_text(message.get("sender_name"))
        if message.get("role") == "user" and name and name not in out:
            out.append(name)
    return out
