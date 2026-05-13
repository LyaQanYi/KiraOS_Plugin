"""
KiraOS Memory WebUI — Self-contained Starlette mini-app (v3.0)

Provides a REST API + single-file SPA over the dual-brain memory system.
Backed by `MemoryManager` (not the v2 SQLite schema).

Uses only uvicorn + starlette, zero extra deps.

Endpoints:
    GET    /                                     — SPA shell
    GET    /api/stats                            — counts (entities, facts, reflections)
    GET    /api/entities[?type=user|group|...]   — list entities
    GET    /api/entity/{type}/{id}               — profile + facts + reflections
    PUT    /api/entity/{type}/{id}/profile       — update profile fields
    POST   /api/entity/{type}/{id}/facts         — add a fact memory
    DELETE /api/entity/{type}/{id}               — delete entity dir (recursive archive)
    PUT    /api/memory/{type}/{id}/{folder}/{memory_id}   — edit one memory's text/importance
    DELETE /api/memory/{type}/{id}/{folder}/{memory_id}   — archive one memory
    GET    /api/search?q=...&entity_id=...&k=10  — recall search
"""

import asyncio
import hashlib
import json
import logging
import os
import secrets
import shutil
import time
from pathlib import Path
from typing import Optional

import uvicorn
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, HTMLResponse, Response
from starlette.routing import Route

from core.logging_manager import get_logger

from .memory import MemoryManager
from .memory.memory_paths import (
    get_entities_dir,
    get_entity_dir,
    list_all_entities,
    _id_to_path_segment,
    ENTITY_USER,
    VALID_ENTITY_TYPES,
)

logger = get_logger("kiraos_webui", "cyan")

_WEB_DIR = Path(__file__).parent / "web"
MAX_TEXT_LEN = 4000


def _mask_id(value: str) -> str:
    if not value:
        return "<empty>"
    h = hashlib.sha256(str(value).encode()).hexdigest()[:8]
    prefix = str(value)[:3] if len(str(value)) >= 3 else str(value)
    return f"{prefix}***({h})"


# ════════════════════════════════════════════════════════════════════
#  Auth Middleware
# ════════════════════════════════════════════════════════════════════

class TokenAuthMiddleware(BaseHTTPMiddleware):
    """Optional Bearer-token authentication via Authorization header."""

    def __init__(self, app, token: str = ""):
        super().__init__(app)
        self.token = token
        self._expected_header = f"Bearer {token}".encode("utf-8") if token else b""

    async def dispatch(self, request: Request, call_next):
        if not self.token or request.url.path == "/":
            return await call_next(request)
        auth = request.headers.get("authorization", "").encode("utf-8")
        if auth and secrets.compare_digest(auth, self._expected_header):
            return await call_next(request)
        return JSONResponse({"error": "Unauthorized"}, status_code=401)


# ════════════════════════════════════════════════════════════════════
#  Helpers
# ════════════════════════════════════════════════════════════════════

def _get_manager(request: Request) -> Optional[MemoryManager]:
    return getattr(request.app.state, "memory_manager", None)


def _validate_entity_type(entity_type: str) -> Optional[str]:
    """Reject unknown types; default empty to 'user'."""
    if not entity_type:
        return ENTITY_USER
    if entity_type not in VALID_ENTITY_TYPES:
        return None
    return entity_type


def _memory_to_dict(mem) -> dict:
    """Serialize a Memory object to JSON-safe dict."""
    return {
        "id": mem.id,
        "type": mem.type,
        "text": mem.raw_text,
        "importance": mem.importance,
        "tags": list(mem.tags or []),
        "source": dict(mem.source or {}),
        "entity_id": getattr(mem, "_entity_id", ""),
        "entity_type": getattr(mem, "_entity_type", ""),
        "folder": getattr(mem, "_folder", ""),
        "access_count": mem.access_count,
        "last_accessed": mem.last_accessed,
        "timestamp": mem.timestamp,
        "file_path": mem.file_path,
    }


# ════════════════════════════════════════════════════════════════════
#  API Handlers
# ════════════════════════════════════════════════════════════════════

async def serve_index(request: Request) -> Response:
    index_path = _WEB_DIR / "index.html"
    if not index_path.is_file():
        return HTMLResponse("<h1>KiraOS Memory WebUI</h1><p>index.html not found.</p>", status_code=404)
    return HTMLResponse(index_path.read_text(encoding="utf-8"))


async def api_stats(request: Request) -> JSONResponse:
    manager = _get_manager(request)
    if not manager:
        return JSONResponse({"error": "memory not ready"}, status_code=503)
    entities = list_all_entities()
    by_type: dict[str, int] = {}
    for _eid, et in entities:
        by_type[et] = by_type.get(et, 0) + 1
    total_facts = manager.memory_index.count_memories(folder="facts")
    total_reflections = manager.memory_index.count_memories(folder="reflections")
    return JSONResponse({
        "entity_count": len(entities),
        "entities_by_type": by_type,
        "fact_count": total_facts,
        "reflection_count": total_reflections,
    })


async def api_list_entities(request: Request) -> JSONResponse:
    manager = _get_manager(request)
    if not manager:
        return JSONResponse({"error": "memory not ready"}, status_code=503)

    filter_type = request.query_params.get("type") or None
    if filter_type and filter_type not in VALID_ENTITY_TYPES:
        return JSONResponse({"error": f"invalid type: {filter_type}"}, status_code=400)

    rows = list_all_entities(filter_type)
    result = []
    for eid, et in rows:
        try:
            profile = await manager.profile_store.get_profile(eid, et)
            label = profile.name or profile.nickname or eid
            result.append({
                "entity_id": eid,
                "entity_type": et,
                "label": label,
                "interaction_count": profile.interaction_count,
                "last_interaction": profile.last_interaction,
            })
        except Exception as e:
            logger.warning(f"Failed to load profile for {_mask_id(eid)}: {e}")
            result.append({
                "entity_id": eid,
                "entity_type": et,
                "label": eid,
                "interaction_count": 0,
                "last_interaction": 0,
            })
    return JSONResponse({"entities": result})


async def api_get_entity(request: Request) -> JSONResponse:
    manager = _get_manager(request)
    if not manager:
        return JSONResponse({"error": "memory not ready"}, status_code=503)

    entity_type = _validate_entity_type(request.path_params["entity_type"])
    if entity_type is None:
        return JSONResponse({"error": "invalid entity_type"}, status_code=400)
    entity_id = request.path_params["entity_id"]

    try:
        profile = await manager.profile_store.get_profile(entity_id, entity_type)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    facts = manager.memory_index.list_memories(
        entity_id=entity_id, entity_type=entity_type, folder="facts"
    )
    reflections = manager.memory_index.list_memories(
        entity_id=entity_id, entity_type=entity_type, folder="reflections"
    )

    return JSONResponse({
        "entity_id": entity_id,
        "entity_type": entity_type,
        "profile": profile.to_dict(),
        "facts": facts,
        "reflections": reflections,
    })


async def api_update_profile(request: Request) -> JSONResponse:
    manager = _get_manager(request)
    if not manager:
        return JSONResponse({"error": "memory not ready"}, status_code=503)

    entity_type = _validate_entity_type(request.path_params["entity_type"])
    if entity_type is None:
        return JSONResponse({"error": "invalid entity_type"}, status_code=400)
    entity_id = request.path_params["entity_id"]

    try:
        payload = await request.json()
    except json.JSONDecodeError:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)

    allowed = {"name", "nickname", "description", "platform"}
    updates = {k: v for k, v in payload.items() if k in allowed and isinstance(v, str)}
    if not updates:
        return JSONResponse({"error": "no valid fields"}, status_code=400)

    try:
        await manager.profile_store.update_profile(entity_id, entity_type, **updates)
    except Exception as e:
        logger.exception("update_profile failed for %s/%s", entity_type, _mask_id(entity_id))
        return JSONResponse({"error": str(e)}, status_code=500)

    return JSONResponse({"ok": True})


async def api_add_fact(request: Request) -> JSONResponse:
    manager = _get_manager(request)
    if not manager:
        return JSONResponse({"error": "memory not ready"}, status_code=503)

    entity_type = _validate_entity_type(request.path_params["entity_type"])
    if entity_type is None:
        return JSONResponse({"error": "invalid entity_type"}, status_code=400)
    entity_id = request.path_params["entity_id"]

    try:
        payload = await request.json()
    except json.JSONDecodeError:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)

    text = (payload.get("text") or "").strip()
    if not text:
        return JSONResponse({"error": "text is required"}, status_code=400)
    if len(text) > MAX_TEXT_LEN:
        return JSONResponse({"error": f"text too long (>{MAX_TEXT_LEN})"}, status_code=400)
    try:
        importance = max(1, min(10, int(payload.get("importance", 5))))
    except (TypeError, ValueError):
        importance = 5
    tags = payload.get("tags", []) or []
    if not isinstance(tags, list):
        return JSONResponse({"error": "tags must be a list"}, status_code=400)

    try:
        mem = await manager.tree_store.add_memory(
            content_text=text,
            memory_type="fact",
            importance=importance,
            tags=[str(t) for t in tags],
            entity_id=entity_id,
            entity_type=entity_type,
            folder="facts",
        )
    except Exception as e:
        logger.exception("add_fact failed")
        return JSONResponse({"error": str(e)}, status_code=500)

    return JSONResponse({"ok": True, "memory_id": mem.id})


async def api_delete_entity(request: Request) -> JSONResponse:
    manager = _get_manager(request)
    if not manager:
        return JSONResponse({"error": "memory not ready"}, status_code=503)

    entity_type = _validate_entity_type(request.path_params["entity_type"])
    if entity_type is None:
        return JSONResponse({"error": "invalid entity_type"}, status_code=400)
    entity_id = request.path_params["entity_id"]

    base_dir = get_entity_dir(entity_id, entity_type)
    if not os.path.isdir(base_dir):
        return JSONResponse({"error": "entity not found"}, status_code=404)

    # Move the whole entity dir into archive/ to keep raw data recoverable.
    # Order matters: clear index rows FIRST so a crash between the two steps
    # leaves at worst orphan archived files (recoverable) rather than stale
    # index rows pointing into a moved-away directory (would surface as ghost
    # search results that then fail to open).
    try:
        # 1. Drop index rows for this entity so search/recall stop surfacing them.
        for folder in ("facts", "reflections"):
            for row in manager.memory_index.list_memories(
                entity_id=entity_id, entity_type=entity_type, folder=folder
            ):
                manager.memory_index.delete(row.get("id"))
        # 2. Move the on-disk entity dir into archive. Use wall-clock time.time()
        #    — `asyncio.get_event_loop().time()` returns a monotonic clock that
        #    resets across restarts and can produce colliding archive names.
        archive_root = Path(get_entities_dir()).parent / "archive" / "_full_entities"
        archive_root.mkdir(parents=True, exist_ok=True)
        target = archive_root / f"{entity_type}_{_id_to_path_segment(entity_id)}_{int(time.time())}"
        shutil.move(base_dir, target)
    except Exception as e:
        logger.exception("delete_entity failed")
        return JSONResponse({"error": str(e)}, status_code=500)

    return JSONResponse({"ok": True})


async def api_update_memory(request: Request) -> JSONResponse:
    manager = _get_manager(request)
    if not manager:
        return JSONResponse({"error": "memory not ready"}, status_code=503)

    entity_type = _validate_entity_type(request.path_params["entity_type"])
    if entity_type is None:
        return JSONResponse({"error": "invalid entity_type"}, status_code=400)
    entity_id = request.path_params["entity_id"]
    folder = request.path_params["folder"]
    memory_id = request.path_params["memory_id"]

    try:
        payload = await request.json()
    except json.JSONDecodeError:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)

    memory = await manager.tree_store.get_memory(
        memory_id=memory_id,
        entity_id=entity_id,
        entity_type=entity_type,
        folder=folder,
    )
    if not memory:
        return JSONResponse({"error": "memory not found"}, status_code=404)

    if "text" in payload and isinstance(payload["text"], str):
        if len(payload["text"]) > MAX_TEXT_LEN:
            return JSONResponse({"error": f"text too long (>{MAX_TEXT_LEN})"}, status_code=400)
        memory.text = payload["text"]
    if "importance" in payload:
        try:
            memory.importance = max(1, min(10, int(payload["importance"])))
        except (TypeError, ValueError):
            pass
    if "tags" in payload and isinstance(payload["tags"], list):
        memory.tags = [str(t) for t in payload["tags"]]

    ok = await manager.tree_store.update_memory(memory)
    return JSONResponse({"ok": bool(ok)})


async def api_delete_memory(request: Request) -> JSONResponse:
    manager = _get_manager(request)
    if not manager:
        return JSONResponse({"error": "memory not ready"}, status_code=503)

    entity_type = _validate_entity_type(request.path_params["entity_type"])
    if entity_type is None:
        return JSONResponse({"error": "invalid entity_type"}, status_code=400)
    entity_id = request.path_params["entity_id"]
    folder = request.path_params["folder"]
    memory_id = request.path_params["memory_id"]

    ok = await manager.tree_store.archive_memory(
        memory_id=memory_id,
        entity_id=entity_id,
        entity_type=entity_type,
        folder=folder,
    )
    return JSONResponse({"ok": bool(ok)})


async def api_search(request: Request) -> JSONResponse:
    manager = _get_manager(request)
    if not manager:
        return JSONResponse({"error": "memory not ready"}, status_code=503)

    query = request.query_params.get("q", "").strip()
    if not query:
        return JSONResponse({"error": "q is required"}, status_code=400)
    entity_id = request.query_params.get("entity_id", "")
    raw_etype = request.query_params.get("entity_type", "")
    # Treat empty entity_type as "any" instead of defaulting to ENTITY_USER —
    # the frontend uses an empty value to mean "search across user/group/channel".
    if raw_etype and raw_etype not in VALID_ENTITY_TYPES:
        return JSONResponse({"error": "invalid entity_type"}, status_code=400)
    entity_type = raw_etype  # "" or a valid type
    try:
        k = max(1, min(50, int(request.query_params.get("k", 10))))
    except (TypeError, ValueError):
        k = 10

    # When neither entity_id nor entity_type is pinned, fan out across every
    # known entity so the user can do a true global recall. The per-entity
    # recall keeps doing its own scoring; we merge by descending importance
    # (a cheap heuristic that matches the single-entity ordering well enough).
    if not entity_id and not entity_type:
        results = []
        for eid, etype in list_all_entities():
            try:
                hits = await manager.recall(
                    query=query, entity_id=eid, entity_type=etype, k=k
                )
                results.extend(hits)
            except Exception as e:
                logger.warning(f"global recall failed for {etype}:{eid}: {e}")
        results.sort(
            key=lambda m: (m.importance, m.last_accessed), reverse=True
        )
        memories = results[:k]
    else:
        memories = await manager.recall(
            query=query,
            entity_id=entity_id,
            entity_type=entity_type or ENTITY_USER,
            k=k,
        )
    return JSONResponse({"memories": [_memory_to_dict(m) for m in memories]})


# ════════════════════════════════════════════════════════════════════
#  Polling log filter
# ════════════════════════════════════════════════════════════════════

class _PollLogFilter(logging.Filter):
    """Drop access-log records for high-frequency GET polling paths."""
    _QUIET_EXACT = frozenset({'/api/stats', '/api/entities'})

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        if '"GET ' not in msg:
            return True
        for path in self._QUIET_EXACT:
            marker = f'"GET {path} '
            if marker in msg or f'"GET {path}?' in msg:
                return False
        return True


# ════════════════════════════════════════════════════════════════════
#  App Factory & Server Management
# ════════════════════════════════════════════════════════════════════

def create_app(memory_manager: MemoryManager, token: str = "") -> Starlette:
    """Build the Starlette app for the WebUI."""
    routes = [
        Route("/", serve_index, methods=["GET"]),
        Route("/api/stats", api_stats, methods=["GET"]),
        Route("/api/entities", api_list_entities, methods=["GET"]),
        Route("/api/search", api_search, methods=["GET"]),
        Route("/api/entity/{entity_type}/{entity_id}", api_get_entity, methods=["GET"]),
        Route("/api/entity/{entity_type}/{entity_id}", api_delete_entity, methods=["DELETE"]),
        Route("/api/entity/{entity_type}/{entity_id}/profile", api_update_profile, methods=["PUT"]),
        Route("/api/entity/{entity_type}/{entity_id}/facts", api_add_fact, methods=["POST"]),
        Route(
            "/api/memory/{entity_type}/{entity_id}/{folder}/{memory_id}",
            api_update_memory,
            methods=["PUT"],
        ),
        Route(
            "/api/memory/{entity_type}/{entity_id}/{folder}/{memory_id}",
            api_delete_memory,
            methods=["DELETE"],
        ),
    ]

    middleware = []
    if token:
        middleware.append(Middleware(TokenAuthMiddleware, token=token))

    app = Starlette(routes=routes, middleware=middleware)
    app.state.memory_manager = memory_manager
    return app


class WebUIServer:
    """Manages the uvicorn server lifecycle for the memory WebUI."""

    def __init__(
        self,
        memory_manager: MemoryManager,
        host: str = "127.0.0.1",
        port: int = 8765,
        token: str = "",
    ):
        self.memory_manager = memory_manager
        self.host = host
        self.port = port
        self.token = token
        self._server: Optional[uvicorn.Server] = None
        self._task: Optional[asyncio.Task] = None
        self._original_handler = None
        self._poll_log_filter: Optional[_PollLogFilter] = None

    async def start(self):
        """Start the web server in a background asyncio task."""
        app = create_app(self.memory_manager, self.token)
        config = uvicorn.Config(
            app,
            host=self.host,
            port=self.port,
            log_level="warning",
            access_log=True,
        )
        self._server = uvicorn.Server(config)

        self._poll_log_filter = _PollLogFilter()
        access_logger = logging.getLogger("uvicorn.access")
        access_logger.addFilter(self._poll_log_filter)

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
                logger.info(f"KiraOS Memory WebUI started at http://{self.host}:{self.port}")
                return
            await asyncio.sleep(0.05)

        logger.warning(
            f"KiraOS WebUI did not report ready within 5s on {self.host}:{self.port}; "
            "continuing anyway"
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
