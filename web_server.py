"""KiraOS Memory WebUI — Starlette mini-app over MemoryManager.

REST endpoints:
  GET    /api/stats                        — entity / memory counts
  GET    /api/entities?type=user|group     — list entities
  GET    /api/entity/{type}/{id}           — profile + per-folder memories
  GET    /api/memory/{id}                  — single memory detail
  PUT    /api/memory/{id}                  — edit text / importance / tags
  DELETE /api/memory/{id}                  — archive (soft delete)
  POST   /api/search                       — body: {query, entity_id?, k?}
  POST   /api/gc                           — run a forgetting cycle on demand

Auth: optional ``Authorization: Bearer <token>`` (set via plugin config).
The SPA at ``/`` reads the token from a one-off ``?token=`` query string,
moves it into ``sessionStorage``, and rewrites the URL.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import secrets
from pathlib import Path
from typing import Any, Optional

import uvicorn
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, HTMLResponse, Response
from starlette.routing import Route

from core.logging_manager import get_logger

from .memory.memory_manager import MemoryManager
from .memory.memory_paths import (
    ENTITIES_DIR,
    list_all_entities,
    get_entity_profile_path,
)

logger = get_logger("kiraos_webui", "cyan")

_WEB_DIR = Path(__file__).parent / "web"


def _mask_id(value: str) -> str:
    if not value:
        return "<empty>"
    h = hashlib.sha256(value.encode()).hexdigest()[:8]
    prefix = value[:3] if len(value) >= 3 else value
    return f"{prefix}***({h})"


# ════════════════════════════════════════════════════════════════════
# Auth middleware (Bearer token)
# ════════════════════════════════════════════════════════════════════


class TokenAuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, token: str = ""):
        super().__init__(app)
        self.token = token
        self._expected_header = (
            f"Bearer {token}".encode("utf-8") if token else b""
        )

    async def dispatch(self, request: Request, call_next):
        # The SPA shell is static, so we let it through without auth — the
        # client-side JS reads the token from ?token= on first paint.
        if not self.token or request.url.path == "/":
            return await call_next(request)
        auth = request.headers.get("authorization", "").encode("utf-8")
        if auth and secrets.compare_digest(auth, self._expected_header):
            return await call_next(request)
        return JSONResponse({"error": "Unauthorized"}, status_code=401)


# ════════════════════════════════════════════════════════════════════
# Log filter: silence repetitive polling lines from the access log
# ════════════════════════════════════════════════════════════════════


class _PollLogFilter(logging.Filter):
    """Drop /api/stats access lines so the log doesn't drown in polls."""

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return "/api/stats" not in msg


# ════════════════════════════════════════════════════════════════════
# Route handlers
# ════════════════════════════════════════════════════════════════════


def _make_handlers(manager: MemoryManager):
    """Closure-capture the manager so route functions stay arg-clean."""

    async def index(_request: Request) -> Response:
        index_path = _WEB_DIR / "index.html"
        if not index_path.exists():
            return HTMLResponse(
                "<h1>KiraOS WebUI</h1><p>index.html not found.</p>",
                status_code=500,
            )
        return HTMLResponse(index_path.read_text(encoding="utf-8"))

    async def stats(_request: Request) -> Response:
        entities = list_all_entities()
        by_type: dict[str, int] = {}
        for _eid, etype in entities:
            by_type[etype] = by_type.get(etype, 0) + 1

        # Folder-wise memory counts come straight out of the SQLite index.
        index = manager.index
        total_memories = index.count_memories()
        facts = index.count_memories(folder="facts")
        reflections = index.count_memories(folder="reflections")

        return JSONResponse({
            "entities": len(entities),
            "by_type": by_type,
            "total_memories": total_memories,
            "facts": facts,
            "reflections": reflections,
            "vec_available": index._vec_available,
        })

    async def entities(request: Request) -> Response:
        etype_filter = request.query_params.get("type")
        rows = list_all_entities(etype_filter)
        out = []
        for eid, etype in rows:
            try:
                profile = await manager.profile_store.get_profile(eid, etype)
                name = profile.name or profile.nickname or eid
            except Exception:
                name = eid
            fact_count = manager.index.count_memories(
                entity_id=eid, entity_type=etype, folder="facts"
            )
            reflection_count = manager.index.count_memories(
                entity_id=eid, entity_type=etype, folder="reflections"
            )
            out.append({
                "entity_id": eid,
                "entity_type": etype,
                "name": name,
                "fact_count": fact_count,
                "reflection_count": reflection_count,
            })
        out.sort(key=lambda r: (r["entity_type"], r["entity_id"]))
        return JSONResponse({"entities": out})

    async def entity_detail(request: Request) -> Response:
        etype = request.path_params["type"]
        eid = request.path_params["id"]

        try:
            profile = await manager.profile_store.get_profile(eid, etype)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        except Exception as e:
            logger.warning(f"profile read error: {e}")
            profile = None

        facts = await manager.tree_store.get_all_memories(
            entity_id=eid, entity_type=etype, folder="facts"
        )
        reflections = await manager.tree_store.get_all_memories(
            entity_id=eid, entity_type=etype, folder="reflections"
        )

        return JSONResponse({
            "entity_id": eid,
            "entity_type": etype,
            "profile": profile.to_dict() if profile else None,
            "facts": [_mem_to_json(m) for m in facts],
            "reflections": [_mem_to_json(m) for m in reflections],
        })

    async def memory_detail(request: Request) -> Response:
        mem_id = request.path_params["id"]
        meta = manager.index.get_meta(mem_id)
        if not meta:
            return JSONResponse({"error": "Memory not found"}, status_code=404)
        # Try to load full TOML content too
        mem = None
        try:
            mem = await manager.tree_store.get_memory(
                memory_id=mem_id,
                entity_id=meta.get("entity_id", ""),
                entity_type=meta.get("entity_type", ""),
                folder=meta.get("folder", "facts"),
                base_dir=meta.get("base_dir", ""),
            )
        except Exception as e:
            logger.warning(f"memory_detail read error: {e}")
        return JSONResponse({
            "meta": meta,
            "memory": _mem_to_json(mem) if mem else None,
        })

    async def memory_update_route(request: Request) -> Response:
        mem_id = request.path_params["id"]
        try:
            payload = await request.json()
        except (json.JSONDecodeError, ValueError):
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)

        meta = manager.index.get_meta(mem_id)
        if not meta:
            return JSONResponse({"error": "Memory not found"}, status_code=404)

        mem = await manager.tree_store.get_memory(
            memory_id=mem_id,
            entity_id=meta.get("entity_id", ""),
            entity_type=meta.get("entity_type", ""),
            folder=meta.get("folder", "facts"),
            base_dir=meta.get("base_dir", ""),
        )
        if not mem:
            return JSONResponse({"error": "Memory file missing"}, status_code=404)

        if "text" in payload and isinstance(payload["text"], str):
            mem.text = payload["text"]
        if "importance" in payload:
            try:
                mem.importance = max(1, min(10, int(payload["importance"])))
            except (TypeError, ValueError):
                pass
        if "tags" in payload and isinstance(payload["tags"], list):
            mem.tags = [str(t) for t in payload["tags"]]

        ok = await manager.tree_store.update_memory(mem)
        return JSONResponse({"ok": ok, "memory": _mem_to_json(mem)})

    async def memory_delete_route(request: Request) -> Response:
        mem_id = request.path_params["id"]
        meta = manager.index.get_meta(mem_id)
        if not meta:
            return JSONResponse({"error": "Memory not found"}, status_code=404)
        ok = await manager.tree_store.archive_memory(
            memory_id=mem_id,
            entity_id=meta.get("entity_id", ""),
            entity_type=meta.get("entity_type", ""),
            folder=meta.get("folder", "facts"),
            base_dir=meta.get("base_dir", ""),
        )
        return JSONResponse({"archived": ok})

    async def search_route(request: Request) -> Response:
        try:
            payload = await request.json()
        except (json.JSONDecodeError, ValueError):
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)

        query = (payload.get("query") or "").strip()
        if not query:
            return JSONResponse({"error": "query is required"}, status_code=400)
        entity_id = payload.get("entity_id") or ""
        entity_type = payload.get("entity_type") or "user"
        try:
            k = max(1, min(50, int(payload.get("k", 10))))
        except (TypeError, ValueError):
            k = 10

        if entity_id:
            memories = await manager.recall(
                query=query,
                entity_id=entity_id,
                entity_type=entity_type,
                k=k,
            )
        else:
            # Index-level FTS5 across everything (no entity filter)
            raw = manager.index.fts_search(query, k=k)
            memories = []
            for r in raw:
                m = await manager.tree_store.get_memory(
                    memory_id=r["id"],
                    entity_id=r.get("entity_id", ""),
                    entity_type=r.get("entity_type", ""),
                    folder=r.get("folder", "facts"),
                    base_dir=r.get("base_dir", ""),
                )
                if m:
                    memories.append(m)

        return JSONResponse({
            "results": [_mem_to_json(m) for m in memories],
        })

    async def gc_route(_request: Request) -> Response:
        removed, downgraded = await manager.run_forgetting_cycle()
        return JSONResponse({"removed": removed, "downgraded": downgraded})

    return {
        "index": index,
        "stats": stats,
        "entities": entities,
        "entity_detail": entity_detail,
        "memory_detail": memory_detail,
        "memory_update": memory_update_route,
        "memory_delete": memory_delete_route,
        "search": search_route,
        "gc": gc_route,
    }


def _mem_to_json(mem) -> dict[str, Any]:
    if mem is None:
        return {}
    return {
        "id": mem.id,
        "type": mem.type,
        "text": mem.text,
        "importance": mem.importance,
        "tags": list(mem.tags),
        "source": dict(mem.source),
        "entity_id": getattr(mem, "_entity_id", ""),
        "entity_type": getattr(mem, "_entity_type", ""),
        "folder": getattr(mem, "_folder", ""),
        "access_count": mem.access_count,
        "last_accessed": mem.last_accessed,
    }


# ════════════════════════════════════════════════════════════════════
# App factory
# ════════════════════════════════════════════════════════════════════


def create_app(manager: MemoryManager, token: str = "") -> Starlette:
    h = _make_handlers(manager)
    routes = [
        Route("/", h["index"], methods=["GET"]),
        Route("/api/stats", h["stats"], methods=["GET"]),
        Route("/api/entities", h["entities"], methods=["GET"]),
        Route("/api/entity/{type:str}/{id:str}", h["entity_detail"], methods=["GET"]),
        Route("/api/memory/{id:str}", h["memory_detail"], methods=["GET"]),
        Route("/api/memory/{id:str}", h["memory_update"], methods=["PUT"]),
        Route("/api/memory/{id:str}", h["memory_delete"], methods=["DELETE"]),
        Route("/api/search", h["search"], methods=["POST"]),
        Route("/api/gc", h["gc"], methods=["POST"]),
    ]
    middleware = [Middleware(TokenAuthMiddleware, token=token)] if token else []
    return Starlette(routes=routes, middleware=middleware)


# ════════════════════════════════════════════════════════════════════
# Server lifecycle
# ════════════════════════════════════════════════════════════════════


class WebUIServer:
    def __init__(
        self,
        memory_manager: MemoryManager,
        host: str = "127.0.0.1",
        port: int = 8765,
        token: str = "",
    ):
        self.manager = memory_manager
        self.host = host
        self.port = port
        self.token = token
        self._server: Optional[uvicorn.Server] = None
        self._task: Optional[asyncio.Task] = None
        self._original_handler = None
        self._poll_log_filter: Optional[_PollLogFilter] = None

    async def start(self):
        app = create_app(self.manager, self.token)
        config = uvicorn.Config(
            app,
            host=self.host,
            port=self.port,
            log_level="warning",
            access_log=True,
        )
        self._server = uvicorn.Server(config)

        self._poll_log_filter = _PollLogFilter()
        logging.getLogger("uvicorn.access").addFilter(self._poll_log_filter)

        loop = asyncio.get_running_loop()
        self._original_handler = loop.get_exception_handler()
        loop.set_exception_handler(self._quiet_exception_handler)

        server = self._server

        async def _serve_safely():
            try:
                await server.serve()
            except SystemExit as e:
                raise RuntimeError(f"uvicorn exited with code {e.code}") from e

        self._task = asyncio.create_task(_serve_safely())

        deadline = loop.time() + 5.0
        while loop.time() < deadline:
            if self._task.done():
                exc = self._task.exception()
                self._task = None
                self._cleanup_log_handlers()
                if exc is not None:
                    raise RuntimeError(
                        f"KiraOS WebUI failed to start on {self.host}:{self.port}: {exc}"
                    ) from exc
                raise RuntimeError(
                    f"KiraOS WebUI exited immediately on {self.host}:{self.port}"
                )
            if getattr(self._server, "started", False):
                logger.info(
                    f"KiraOS Memory WebUI started at http://{self.host}:{self.port}"
                )
                return
            await asyncio.sleep(0.05)

        logger.warning(
            f"KiraOS WebUI did not report ready within 5s on "
            f"{self.host}:{self.port}; continuing anyway"
        )

    def _cleanup_log_handlers(self):
        if self._poll_log_filter:
            logging.getLogger("uvicorn.access").removeFilter(self._poll_log_filter)
            self._poll_log_filter = None
        try:
            loop = asyncio.get_running_loop()
            loop.set_exception_handler(self._original_handler)
        except RuntimeError:
            pass
        self._original_handler = None

    def _quiet_exception_handler(self, loop, context):
        exc = context.get("exception")
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError)):
            return
        if self._original_handler:
            self._original_handler(loop, context)
        else:
            loop.default_exception_handler(context)

    async def stop(self):
        if self._server:
            self._server.should_exit = True
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()
            self._task = None
        self._server = None

        self._cleanup_log_handlers()
        logger.info("KiraOS Memory WebUI stopped")
