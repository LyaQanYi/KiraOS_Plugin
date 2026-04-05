"""
KiraOS Memory WebUI — Self-contained Starlette mini-app.

Provides a REST API for managing user memories (profiles & events)
and serves a single-file SPA for visual management.

Uses only uvicorn + starlette (shipped with FastAPI, zero extra deps).
"""

import asyncio
import json
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

logger = get_logger("kiraos_webui", "cyan")

_WEB_DIR = Path(__file__).parent / "web"


# ════════════════════════════════════════════════════════════════════
#  Auth Middleware
# ════════════════════════════════════════════════════════════════════

class TokenAuthMiddleware(BaseHTTPMiddleware):
    """Optional Bearer-token authentication."""

    def __init__(self, app, token: str = ""):
        super().__init__(app)
        self.token = token

    async def dispatch(self, request: Request, call_next):
        # Skip auth if no token configured or if requesting the SPA page
        if not self.token or request.url.path == "/":
            return await call_next(request)

        # Check Authorization header
        auth = request.headers.get("authorization", "")
        if auth == f"Bearer {self.token}":
            return await call_next(request)

        # Check query parameter (for initial page load)
        if request.query_params.get("token") == self.token:
            return await call_next(request)

        return JSONResponse({"error": "Unauthorized"}, status_code=401)


# ════════════════════════════════════════════════════════════════════
#  API Handlers
# ════════════════════════════════════════════════════════════════════

def _get_db(request: Request):
    """Get the UserMemoryDB instance from app state."""
    db = request.app.state.db
    if db is None:
        return None
    return db


async def serve_index(request: Request) -> Response:
    """Serve the SPA index.html."""
    index_path = _WEB_DIR / "index.html"
    if not index_path.is_file():
        return HTMLResponse("<h1>KiraOS Memory WebUI</h1><p>index.html not found.</p>", status_code=404)
    return HTMLResponse(index_path.read_text(encoding="utf-8"))


async def api_list_users(request: Request) -> JSONResponse:
    """GET /api/users — List all users with memory data."""
    db = _get_db(request)
    if not db:
        return JSONResponse({"error": "Database not available"}, status_code=503)
    try:
        users = db.list_users()
        return JSONResponse(users)
    except Exception as e:
        logger.error(f"Error listing users: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


async def api_get_user(request: Request) -> JSONResponse:
    """GET /api/users/{user_id} — Get all profiles + events for a user."""
    db = _get_db(request)
    if not db:
        return JSONResponse({"error": "Database not available"}, status_code=503)

    user_id = request.path_params["user_id"]

    try:
        profiles = db.get_profiles(user_id, include_expired=True)
        events = db.get_events_with_id(user_id, limit=200)

        profile_list = [
            {
                "key": key,
                "value": value,
                "updated_at": updated_at,
                "confidence": confidence,
                "category": category,
                "expires_at": expires_at,
            }
            for key, value, updated_at, confidence, category, expires_at in profiles
        ]

        event_list = [
            {
                "id": eid,
                "event_summary": summary,
                "created_at": created_at,
            }
            for eid, summary, created_at in events
        ]

        return JSONResponse({
            "user_id": user_id,
            "profiles": profile_list,
            "events": event_list,
        })
    except Exception as e:
        logger.error(f"Error getting user {user_id}: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


async def api_update_profile(request: Request) -> JSONResponse:
    """PUT /api/users/{user_id}/profiles/{key} — Update a profile entry."""
    db = _get_db(request)
    if not db:
        return JSONResponse({"error": "Database not available"}, status_code=503)

    user_id = request.path_params["user_id"]
    key = request.path_params["key"]

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    value = body.get("value", "")
    if not value:
        return JSONResponse({"error": "value is required"}, status_code=400)

    confidence = max(0.0, min(1.0, float(body.get("confidence", 0.5))))
    category = body.get("category", "basic")
    expires_at = body.get("expires_at")

    from .db import VALID_CATEGORIES
    if category not in VALID_CATEGORIES:
        category = "basic"

    try:
        db.save_profile(user_id, key, value,
                        confidence=confidence,
                        category=category,
                        expires_at=expires_at)
        return JSONResponse({"ok": True, "key": key, "value": value})
    except Exception as e:
        logger.error(f"Error updating profile {user_id}/{key}: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


async def api_delete_profile(request: Request) -> JSONResponse:
    """DELETE /api/users/{user_id}/profiles/{key} — Delete a profile entry."""
    db = _get_db(request)
    if not db:
        return JSONResponse({"error": "Database not available"}, status_code=503)

    user_id = request.path_params["user_id"]
    key = request.path_params["key"]

    try:
        removed = db.remove_profile(user_id, key)
        if removed:
            return JSONResponse({"ok": True})
        return JSONResponse({"error": "Profile not found"}, status_code=404)
    except Exception as e:
        logger.error(f"Error deleting profile {user_id}/{key}: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


async def api_delete_event(request: Request) -> JSONResponse:
    """DELETE /api/users/{user_id}/events/{event_id} — Delete a single event."""
    db = _get_db(request)
    if not db:
        return JSONResponse({"error": "Database not available"}, status_code=503)

    try:
        event_id = int(request.path_params["event_id"])
    except ValueError:
        return JSONResponse({"error": "Invalid event_id"}, status_code=400)

    try:
        removed = db.delete_event(event_id)
        if removed:
            return JSONResponse({"ok": True})
        return JSONResponse({"error": "Event not found"}, status_code=404)
    except Exception as e:
        logger.error(f"Error deleting event {event_id}: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


async def api_clear_user(request: Request) -> JSONResponse:
    """DELETE /api/users/{user_id} — Clear all memory for a user."""
    db = _get_db(request)
    if not db:
        return JSONResponse({"error": "Database not available"}, status_code=503)

    user_id = request.path_params["user_id"]

    try:
        profiles_del, events_del = db.clear_user_memory(user_id)
        return JSONResponse({
            "ok": True,
            "profiles_deleted": profiles_del,
            "events_deleted": events_del,
        })
    except Exception as e:
        logger.error(f"Error clearing user {user_id}: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


# ════════════════════════════════════════════════════════════════════
#  App Factory & Server Management
# ════════════════════════════════════════════════════════════════════

def create_app(db, token: str = "") -> Starlette:
    """Create the Starlette app with routes and middleware."""
    # Order matters: more-specific (longer) routes first, then catch-all.
    routes = [
        Route("/", serve_index, methods=["GET"]),
        Route("/api/users/{user_id}/profiles/{key}", api_update_profile, methods=["PUT"]),
        Route("/api/users/{user_id}/profiles/{key}", api_delete_profile, methods=["DELETE"]),
        Route("/api/users/{user_id}/events/{event_id:int}", api_delete_event, methods=["DELETE"]),
        Route("/api/users/{user_id}", api_get_user, methods=["GET"]),
        Route("/api/users/{user_id}", api_clear_user, methods=["DELETE"]),
        Route("/api/users", api_list_users, methods=["GET"]),
    ]

    middleware = []
    if token:
        middleware.append(Middleware(TokenAuthMiddleware, token=token))

    app = Starlette(routes=routes, middleware=middleware)
    app.state.db = db
    return app


class WebUIServer:
    """Manages the uvicorn server lifecycle for the memory WebUI."""

    def __init__(self, db, host: str = "127.0.0.1", port: int = 8765, token: str = ""):
        self.db = db
        self.host = host
        self.port = port
        self.token = token
        self._server: Optional[uvicorn.Server] = None
        self._task: Optional[asyncio.Task] = None
        self._original_handler = None

    async def start(self):
        """Start the web server in a background asyncio task."""
        app = create_app(self.db, self.token)
        config = uvicorn.Config(
            app,
            host=self.host,
            port=self.port,
            log_level="warning",
            access_log=False,
        )
        self._server = uvicorn.Server(config)

        # Suppress Windows ProactorEventLoop ConnectionResetError noise
        # that fires in async callbacks *after* connections close.
        loop = asyncio.get_running_loop()
        self._original_handler = loop.get_exception_handler()
        loop.set_exception_handler(self._quiet_exception_handler)

        self._task = asyncio.create_task(self._server.serve())
        logger.info(f"KiraOS Memory WebUI started at http://{self.host}:{self.port}")

    def _quiet_exception_handler(self, loop, context):
        exc = context.get("exception")
        # Suppress connection-reset errors from ProactorEventLoop on Windows
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError)):
            return
        if self._original_handler:
            self._original_handler(loop, context)
        else:
            loop.default_exception_handler(context)

    async def stop(self):
        """Gracefully shut down the web server."""
        if self._server:
            self._server.should_exit = True
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()
            self._task = None
        self._server = None

        # Restore original exception handler
        try:
            loop = asyncio.get_running_loop()
            loop.set_exception_handler(self._original_handler)
        except RuntimeError:
            pass
        self._original_handler = None
        logger.info("KiraOS Memory WebUI stopped")
