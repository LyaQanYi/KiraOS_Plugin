"""
KiraOS Memory WebUI — Self-contained Starlette mini-app.

Provides a REST API for managing user memories (profiles & events)
and serves a single-file SPA for visual management.

Uses only uvicorn + starlette (shipped with FastAPI, zero extra deps).
"""

import asyncio
import hashlib
import json
import logging
import secrets
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
MAX_EVENT_SUMMARY_LEN = 1000


def _mask_id(value: str) -> str:
    """Return a masked identifier safe for logging (prefix + short hash)."""
    if not value:
        return "<empty>"
    h = hashlib.sha256(value.encode()).hexdigest()[:8]
    prefix = value[:3] if len(value) >= 3 else value
    return f"{prefix}***({h})"


# ════════════════════════════════════════════════════════════════════
#  Auth Middleware
# ════════════════════════════════════════════════════════════════════

class TokenAuthMiddleware(BaseHTTPMiddleware):
    """Optional Bearer-token authentication.

    Compared against the configured token using ``secrets.compare_digest`` so
    request-time differences cannot leak the token via a timing side-channel.

    Only the ``Authorization: Bearer <token>`` header is honoured for API
    requests. The previous ``?token=<token>`` query-param fallback was removed
    because tokens in URLs leak into server access logs, browser history, and
    HTTP ``Referer`` headers. The SPA bootstraps from ``?token=`` on its own
    (it reads it from the URL on first paint, stashes it in sessionStorage,
    rewrites the URL, then sends the Bearer header from then on).
    """

    def __init__(self, app, token: str = ""):
        super().__init__(app)
        self.token = token
        # Prebuild the expected header value once for compare_digest
        self._expected_header = f"Bearer {token}".encode("utf-8") if token else b""

    async def dispatch(self, request: Request, call_next):
        # Skip auth if no token configured or if requesting the SPA shell.
        # The SPA shell is static markup with no embedded data; the token in
        # ?token= is read by JS on the client side, never validated here.
        if not self.token or request.url.path == "/":
            return await call_next(request)

        auth = request.headers.get("authorization", "").encode("utf-8")
        if auth and secrets.compare_digest(auth, self._expected_header):
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


async def api_stats(request: Request) -> JSONResponse:
    """GET /api/stats — Return global statistics."""
    db = _get_db(request)
    if not db:
        return JSONResponse({"error": "Database not available"}, status_code=503)
    try:
        stats = db.get_stats()
        return JSONResponse(stats)
    except Exception:
        logger.exception("Error getting stats")
        return JSONResponse({"error": "Internal server error"}, status_code=500)


async def api_list_users(request: Request) -> JSONResponse:
    """GET /api/users — List all users with memory data.

    Optional ``?q=<term>`` triggers a content search across user_id, profile
    values, and event summaries; the response includes ``match_in`` and
    ``snippet`` fields per row.
    """
    db = _get_db(request)
    if not db:
        return JSONResponse({"error": "Database not available"}, status_code=503)
    try:
        q = (request.query_params.get("q") or "").strip()
        if q:
            users = db.search_users(q, limit=200)
        else:
            users = db.list_users()
        return JSONResponse(users)
    except Exception:
        logger.exception("Error listing users")
        return JSONResponse({"error": "Internal server error"}, status_code=500)


# ── Bulk export / import ──────────────────────────────────────────

# Cap the size of an uploaded import payload so a malicious or runaway
# request can't OOM the server.
MAX_IMPORT_BYTES = 50 * 1024 * 1024  # 50 MB


async def api_export(request: Request) -> JSONResponse:
    """GET /api/export — Dump all users' profiles and events as JSON."""
    db = _get_db(request)
    if not db:
        return JSONResponse({"error": "Database not available"}, status_code=503)
    try:
        snapshot = db.export_all()
        return JSONResponse(
            snapshot,
            headers={
                "Content-Disposition": (
                    f"attachment; filename=kiraos-memory-"
                    f"{snapshot['exported_at'][:10]}.json"
                )
            },
        )
    except Exception:
        logger.exception("Error exporting memory")
        return JSONResponse({"error": "Internal server error"}, status_code=500)


async def api_import(request: Request) -> JSONResponse:
    """POST /api/import — Bulk-load a snapshot produced by /api/export.

    Accepts ``mode=merge|upsert|replace`` (default ``merge``).
    """
    db = _get_db(request)
    if not db:
        return JSONResponse({"error": "Database not available"}, status_code=503)
    cl = request.headers.get("content-length")
    if cl and cl.isdigit() and int(cl) > MAX_IMPORT_BYTES:
        return JSONResponse(
            {"error": f"Import payload too large (>{MAX_IMPORT_BYTES} bytes)"},
            status_code=413,
        )
    try:
        body = await request.body()
    except Exception:
        return JSONResponse({"error": "Failed to read request body"}, status_code=400)
    if len(body) > MAX_IMPORT_BYTES:
        return JSONResponse(
            {"error": f"Import payload too large (>{MAX_IMPORT_BYTES} bytes)"},
            status_code=413,
        )
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
    if not isinstance(data, dict):
        return JSONResponse({"error": "Top-level JSON must be an object"}, status_code=400)
    mode = (request.query_params.get("mode") or "merge").lower()
    if mode not in ("merge", "upsert", "replace"):
        return JSONResponse({"error": f"invalid mode '{mode}'"}, status_code=400)
    try:
        result = db.import_all(data, mode=mode)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception:
        logger.exception("Error importing memory")
        return JSONResponse({"error": "Internal server error"}, status_code=500)
    return JSONResponse({"ok": True, **result})


async def api_get_user(request: Request) -> JSONResponse:
    """GET /api/users/{user_id} — Get all profiles + events for a user."""
    db = _get_db(request)
    if not db:
        return JSONResponse({"error": "Database not available"}, status_code=503)

    user_id = request.path_params["user_id"]

    try:
        profiles = db.get_profiles(user_id, include_expired=True)
        events = db.get_events_with_id(user_id, limit=200)

        from .db import _epoch_to_iso_date
        profile_list = [
            {
                "key": key,
                "value": value,
                "updated_at": updated_at,
                "confidence": confidence,
                "category": category,
                # expires_at is stored as integer epoch; expose as ISO date for the UI
                "expires_at": _epoch_to_iso_date(expires_at),
            }
            for key, value, updated_at, confidence, category, expires_at in profiles
        ]

        event_list = [
            {
                "id": eid,
                "event_summary": summary,
                "created_at": created_at,
                "tag": tag,
            }
            for eid, summary, created_at, tag in events
        ]

        return JSONResponse({
            "user_id": user_id,
            "profiles": profile_list,
            "events": event_list,
        })
    except Exception:
        logger.exception("Error getting user %s", _mask_id(user_id))
        return JSONResponse({"error": "Internal server error"}, status_code=500)


async def api_embeddings_backfill(request: Request) -> JSONResponse:
    """POST /api/embeddings/backfill?dry_run=1[&kinds=event,reflection]

    Drives :func:`embeddings.backfill_async` against the plugin's DB.
    ``dry_run=1`` returns pending counts without writing or even
    resolving an embedding backend — safe to expose in any UI as a
    "what would this do?" preview. ``dry_run=0`` performs the work,
    bounded by ``batch_size`` (default 100) and ``max_total`` (default
    5000) so a single click can't try to embed everything in one shot.

    The embedding service is looked up via app.state.embedding_service
    if the plugin has wired one; otherwise we construct a disabled
    instance so the dry-run path still works and the real-run path
    surfaces a clear "no backend" error.
    """
    db = _get_db(request)
    if not db:
        return JSONResponse({"error": "Database not available"}, status_code=503)

    qp = request.query_params
    dry_run = qp.get("dry_run", "1") not in ("0", "false", "False", "")
    kinds_param = qp.get("kinds")
    kinds: Optional[list[str]]
    if kinds_param:
        kinds = [k.strip() for k in kinds_param.split(",") if k.strip()]
    else:
        kinds = None
    try:
        batch_size = int(qp.get("batch_size", "100"))
    except (TypeError, ValueError):
        batch_size = 100
    batch_size = max(1, min(1000, batch_size))
    try:
        max_total = int(qp.get("max_total", "5000"))
    except (TypeError, ValueError):
        max_total = 5000
    max_total = max(1, min(100_000, max_total))

    # Pull the embedding service if the host plugin attached one.
    # A disabled service still allows dry_run to return useful info.
    service = getattr(request.app.state, "embedding_service", None)
    if service is None:
        # Local fallback — keeps the endpoint self-contained for
        # tests / standalone runs of the web server.
        from .embeddings import EmbeddingService
        service = EmbeddingService(enabled=False)

    try:
        from .embeddings import backfill_async
        report = await backfill_async(
            db, service,
            kinds=kinds,
            batch_size=batch_size,
            max_total=max_total,
            dry_run=dry_run,
        )
    except Exception:
        logger.exception("Backfill failed")
        return JSONResponse({"error": "Internal server error"}, status_code=500)

    return JSONResponse({
        "ok": True,
        "dry_run": report.dry_run,
        "kinds": report.kinds,
        "pending_before": report.pending_before,
        "processed": report.processed,
        "written": report.written,
        "skipped": report.skipped,
        "errors": report.errors[:20],  # cap so a flood of errors doesn't blow the response
    })


async def api_list_reflections(request: Request) -> JSONResponse:
    """GET /api/users/{user_id}/reflections?status= — list reflections.

    Optional ``?status=`` filter passes through to db.list_reflections.
    Always returns the full row dict (id, summary, entity,
    relation_type, source_fact_ids, status, timestamps) so the
    Phase 5 WebUI can render the FSM view without a second round-trip.
    """
    db = _get_db(request)
    if not db:
        return JSONResponse({"error": "Database not available"}, status_code=503)
    user_id = request.path_params["user_id"]
    status = request.query_params.get("status")
    try:
        limit = int(request.query_params.get("limit", "100"))
    except (TypeError, ValueError):
        limit = 100
    limit = max(1, min(1000, limit))
    try:
        rows = db.list_reflections(user_id, status=status, limit=limit)
        return JSONResponse({
            "user_id": user_id,
            "status_filter": status,
            "reflections": rows,
        })
    except Exception:
        logger.exception("Error listing reflections for %s", _mask_id(user_id))
        return JSONResponse({"error": "Internal server error"}, status_code=500)


async def api_promote_reflection(request: Request) -> JSONResponse:
    """POST /api/users/{user_id}/reflections/{rid}/promote — force-promote.

    Bypasses the evidence-threshold check that auto-promotion enforces.
    Designed for the WebUI's manual "I want this in my persona right
    now" button. The reflection's row is upserted into user_profiles
    under key ``reflection_{rid}`` with source='reflection_promote'
    and the FSM transitions to 'promoted'.
    """
    db = _get_db(request)
    if not db:
        return JSONResponse({"error": "Database not available"}, status_code=503)
    user_id = request.path_params["user_id"]
    try:
        rid = int(request.path_params["rid"])
    except (TypeError, ValueError):
        return JSONResponse({"error": "Invalid reflection id"}, status_code=400)

    rows = db.list_reflections(user_id, limit=100_000)
    ref = next((r for r in rows if r["id"] == rid), None)
    if ref is None:
        return JSONResponse({"error": "Reflection not found"}, status_code=404)
    try:
        # Upsert into persona. We don't need the reconciler here — its
        # manual path is just a thin wrapper around the same DB calls
        # we make below. Keeping the web endpoint self-contained means
        # the standalone Starlette server doesn't need a back-reference
        # to the running plugin.
        status, _info = db.upsert_with_limit(
            user_id,
            f"reflection_{rid}", ref["summary"],
            max_profiles=10_000,
            confidence=0.7,
            category="other",
        )
        if status not in ("set", "updated", "truncated"):
            return JSONResponse(
                {"error": f"Promotion failed (status={status})"},
                status_code=409,
            )
        # Stamp Phase 3a metadata + flip FSM.
        with db._write_lock:
            db._get_conn().execute(
                "UPDATE user_profiles SET source = ?, entity = ?, "
                "relation_type = ? "
                "WHERE user_id = ? AND memory_key = ?",
                ("reflection_promote", ref.get("entity"),
                 ref.get("relation_type"), user_id, f"reflection_{rid}"),
            )
            db._get_conn().commit()
        import time as _t
        db.update_reflection_status(rid, "promoted", promoted_at=int(_t.time()))
        return JSONResponse({"ok": True, "reflection_id": rid, "status": "promoted"})
    except Exception:
        logger.exception("Error promoting reflection %d for %s",
                         rid, _mask_id(user_id))
        return JSONResponse({"error": "Internal server error"}, status_code=500)


async def api_deny_reflection(request: Request) -> JSONResponse:
    """POST /api/users/{user_id}/reflections/{rid}/deny — register a
    disputation signal and flip the reflection's status to 'denied'.

    Subsequent auto-promotion sweeps skip denied reflections; the
    evidence ledger entry now has a non-zero disp that decays with
    its own half-life (see EvidenceConfig). If the user later changes
    their mind, the WebUI's manual promote button still works.
    """
    db = _get_db(request)
    if not db:
        return JSONResponse({"error": "Database not available"}, status_code=503)
    user_id = request.path_params["user_id"]
    try:
        rid = int(request.path_params["rid"])
    except (TypeError, ValueError):
        return JSONResponse({"error": "Invalid reflection id"}, status_code=400)

    rows = db.list_reflections(user_id, limit=100_000)
    if not any(r["id"] == rid for r in rows):
        return JSONResponse({"error": "Reflection not found"}, status_code=404)
    try:
        db.record_evidence_signal(
            "reflection", str(rid),
            disp_delta=1.0,
            source="user_directive",
        )
        db.update_reflection_status(rid, "denied")
        return JSONResponse({"ok": True, "reflection_id": rid, "status": "denied"})
    except Exception:
        logger.exception("Error denying reflection %d for %s",
                         rid, _mask_id(user_id))
        return JSONResponse({"error": "Internal server error"}, status_code=500)


async def api_user_recall(request: Request) -> JSONResponse:
    """GET /api/users/{user_id}/recall?q=&k= — BM25-ranked event recall.

    Mirrors the ``memory_query(query=…)`` tool but exposes the
    per-event score and tokenizer source for the WebUI to render
    debugging information. Embedding cosine + LLM rerank are
    intentionally omitted from the HTTP surface — they require the
    plugin's runtime context (LLM client) that the standalone server
    doesn't have access to. Callers wanting the full pipeline should
    use the tool from the LLM side.
    """
    db = _get_db(request)
    if not db:
        return JSONResponse({"error": "Database not available"}, status_code=503)

    user_id = request.path_params["user_id"]
    q = (request.query_params.get("q") or "").strip()
    if not q:
        return JSONResponse(
            {"error": "missing required query parameter 'q'"}, status_code=400
        )
    try:
        k = int(request.query_params.get("k", "20"))
    except (TypeError, ValueError):
        k = 20
    k = max(1, min(100, k))

    try:
        rows = db.search_events_fts(user_id, q, limit=k)
        # The fts5_enabled flag drives the "source" hint so a user
        # debugging "why am I getting LIKE results" can see at a glance
        # whether the trigram path was actually exercised.
        source = "fts5_bm25"
        if not db.fts5_enabled or len(q) < db.FTS_MIN_QUERY_LEN:
            source = "like_fallback"
        return JSONResponse({
            "user_id": user_id,
            "query": q,
            "source": source,
            "results": [
                {
                    "id": eid, "event_summary": summary,
                    "created_at": created_at, "tag": tag, "score": score,
                }
                for eid, summary, created_at, tag, score in rows
            ],
        })
    except Exception:
        logger.exception("Error in recall for %s", _mask_id(user_id))
        return JSONResponse({"error": "Internal server error"}, status_code=500)


async def api_update_profile(request: Request) -> JSONResponse:
    """PUT /api/users/{user_id}/profiles/{key} — Update a profile entry."""
    db = _get_db(request)
    if not db:
        return JSONResponse({"error": "Database not available"}, status_code=503)

    user_id = request.path_params["user_id"]
    key = request.path_params["key"]

    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    value = body.get("value", "")
    if not value:
        return JSONResponse({"error": "value is required"}, status_code=400)

    try:
        confidence = max(0.0, min(1.0, float(body.get("confidence", 0.5))))
    except (ValueError, TypeError):
        return JSONResponse({"error": "Invalid confidence value, must be a number"}, status_code=400)
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
    except Exception:
        logger.exception("Error updating profile %s/%s", _mask_id(user_id), key)
        return JSONResponse({"error": "Internal server error"}, status_code=500)


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
    except Exception:
        logger.exception("Error deleting profile %s/%s", _mask_id(user_id), key)
        return JSONResponse({"error": "Internal server error"}, status_code=500)


async def api_delete_event(request: Request) -> JSONResponse:
    """DELETE /api/users/{user_id}/events/{event_id} — Delete a single event."""
    db = _get_db(request)
    if not db:
        return JSONResponse({"error": "Database not available"}, status_code=503)

    user_id = request.path_params["user_id"]
    try:
        event_id = int(request.path_params["event_id"])
    except ValueError:
        return JSONResponse({"error": "Invalid event_id"}, status_code=400)

    try:
        removed = db.delete_event(event_id, user_id=user_id)
        if removed:
            return JSONResponse({"ok": True})
        return JSONResponse({"error": "Event not found or not owned by this user"}, status_code=404)
    except Exception:
        logger.exception(f"Error deleting event {event_id}")
        return JSONResponse({"error": "Internal server error"}, status_code=500)


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
    except Exception:
        logger.exception("Error clearing user %s", _mask_id(user_id))
        return JSONResponse({"error": "Internal server error"}, status_code=500)


async def api_add_event(request: Request) -> JSONResponse:
    """POST /api/users/{user_id}/events — Add a new event."""
    db = _get_db(request)
    if not db:
        return JSONResponse({"error": "Database not available"}, status_code=503)

    user_id = request.path_params["user_id"]

    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    event_summary = (body.get("event_summary") or "").strip()
    if not event_summary:
        return JSONResponse({"error": "event_summary is required"}, status_code=400)
    if len(event_summary) > MAX_EVENT_SUMMARY_LEN:
        return JSONResponse({"error": f"event_summary must be at most {MAX_EVENT_SUMMARY_LEN} characters"}, status_code=400)
    tag = body.get("tag")
    if tag is not None and not isinstance(tag, str):
        return JSONResponse({"error": "tag must be a string"}, status_code=400)

    try:
        db.save_event(user_id, event_summary, tag=tag)
    except Exception:
        logger.exception("Error adding event for %s", _mask_id(user_id))
        return JSONResponse({"error": "Internal server error"}, status_code=500)

    # Cleanup runs best-effort after successful save
    try:
        max_keep = max(0, int(request.app.state.max_event_keep))
        if max_keep > 0:
            db.cleanup_old_events(user_id, keep=max_keep)
    except (ValueError, TypeError):
        logger.warning("Invalid max_event_keep value, skipping cleanup")
    except Exception:
        logger.exception("Error during event cleanup for %s", _mask_id(user_id))

    return JSONResponse({"ok": True})


async def api_update_event(request: Request) -> JSONResponse:
    """PUT /api/users/{user_id}/events/{event_id} — Update an event's summary."""
    db = _get_db(request)
    if not db:
        return JSONResponse({"error": "Database not available"}, status_code=503)

    user_id = request.path_params["user_id"]
    try:
        event_id = int(request.path_params["event_id"])
    except ValueError:
        return JSONResponse({"error": "Invalid event_id"}, status_code=400)

    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    event_summary = (body.get("event_summary") or "").strip()
    if not event_summary:
        return JSONResponse({"error": "event_summary is required"}, status_code=400)
    if len(event_summary) > MAX_EVENT_SUMMARY_LEN:
        return JSONResponse({"error": f"event_summary must be at most {MAX_EVENT_SUMMARY_LEN} characters"}, status_code=400)
    # Tag is optional; ``"tag" in body`` distinguishes "no change" from "clear".
    tag_provided = "tag" in body
    tag_value = body.get("tag")
    if tag_provided and tag_value is not None and not isinstance(tag_value, str):
        return JSONResponse({"error": "tag must be a string or null"}, status_code=400)

    try:
        updated = db.update_event(
            event_id, event_summary, user_id=user_id,
            tag=tag_value if tag_provided else None,
            set_tag=tag_provided,
        )
        if updated:
            return JSONResponse({"ok": True})
        return JSONResponse({"error": "Event not found or not owned by this user"}, status_code=404)
    except Exception:
        logger.exception(f"Error updating event {event_id}")
        return JSONResponse({"error": "Internal server error"}, status_code=500)


# ════════════════════════════════════════════════════════════════════
#  Logging Filter — suppress noisy polling endpoints
# ════════════════════════════════════════════════════════════════════

class _PollLogFilter(logging.Filter):
    """Drop access-log records for high-frequency GET polling paths."""
    _QUIET_EXACT = frozenset({'/api/stats', '/api/users'})

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        # Uvicorn access log format: '<addr> - "<METHOD> <path> HTTP/..." <status>'
        if '"GET ' not in msg:
            return True
        for path in self._QUIET_EXACT:
            # Match exact path followed by space or query string
            marker = f'"GET {path} '
            if marker in msg or f'"GET {path}?' in msg:
                return False
        return True


# ════════════════════════════════════════════════════════════════════
#  App Factory & Server Management
# ════════════════════════════════════════════════════════════════════

def create_app(db, token: str = "", max_event_keep: int = 0) -> Starlette:
    """Create the Starlette app with routes and middleware."""
    # Order matters: more-specific (longer) routes first, then catch-all.
    routes = [
        Route("/", serve_index, methods=["GET"]),
        Route("/api/users/{user_id}/profiles/{key}", api_update_profile, methods=["PUT"]),
        Route("/api/users/{user_id}/profiles/{key}", api_delete_profile, methods=["DELETE"]),
        Route("/api/users/{user_id}/events/{event_id:int}", api_update_event, methods=["PUT"]),
        Route("/api/users/{user_id}/events/{event_id:int}", api_delete_event, methods=["DELETE"]),
        Route("/api/users/{user_id}/events", api_add_event, methods=["POST"]),
        Route("/api/users/{user_id}/recall", api_user_recall, methods=["GET"]),
        Route("/api/users/{user_id}/reflections", api_list_reflections, methods=["GET"]),
        Route("/api/users/{user_id}/reflections/{rid}/promote", api_promote_reflection, methods=["POST"]),
        Route("/api/users/{user_id}/reflections/{rid}/deny", api_deny_reflection, methods=["POST"]),
        Route("/api/users/{user_id}", api_get_user, methods=["GET"]),
        Route("/api/users/{user_id}", api_clear_user, methods=["DELETE"]),
        Route("/api/export", api_export, methods=["GET"]),
        Route("/api/import", api_import, methods=["POST"]),
        Route("/api/embeddings/backfill", api_embeddings_backfill, methods=["POST"]),
        Route("/api/stats", api_stats, methods=["GET"]),
        Route("/api/users", api_list_users, methods=["GET"]),
    ]

    middleware = []
    if token:
        middleware.append(Middleware(TokenAuthMiddleware, token=token))

    app = Starlette(routes=routes, middleware=middleware)
    app.state.db = db
    app.state.max_event_keep = max_event_keep
    return app


class WebUIServer:
    """Manages the uvicorn server lifecycle for the memory WebUI."""

    def __init__(self, db, host: str = "127.0.0.1", port: int = 8765, token: str = "", max_event_keep: int = 0):
        self.db = db
        self.host = host
        self.port = port
        self.token = token
        self.max_event_keep = max_event_keep
        self._server: Optional[uvicorn.Server] = None
        self._task: Optional[asyncio.Task] = None
        self._original_handler = None
        self._poll_log_filter: Optional[_PollLogFilter] = None

    async def start(self):
        """Start the web server in a background asyncio task.

        Raises ``RuntimeError`` if the server fails to come up within a short
        readiness window — typically because the configured port is already
        bound. Without this check the failure was silent: ``serve()`` raised
        inside the task, the plugin logged 'started', and every subsequent
        request returned a connection error.
        """
        app = create_app(self.db, self.token, max_event_keep=self.max_event_keep)
        config = uvicorn.Config(
            app,
            host=self.host,
            port=self.port,
            log_level="warning",
            access_log=True,
        )
        self._server = uvicorn.Server(config)

        # Suppress repetitive access logs from high-frequency polling endpoints
        self._poll_log_filter = _PollLogFilter()
        access_logger = logging.getLogger("uvicorn.access")
        access_logger.addFilter(self._poll_log_filter)

        # Suppress Windows ProactorEventLoop ConnectionResetError noise
        # that fires in async callbacks *after* connections close.
        loop = asyncio.get_running_loop()
        self._original_handler = loop.get_exception_handler()
        loop.set_exception_handler(self._quiet_exception_handler)

        # uvicorn calls ``sys.exit(1)`` when it can't bind the port. ``SystemExit``
        # is a ``BaseException`` and asyncio propagates it straight out of
        # ``run_until_complete``, so a plain ``await self._server.serve()`` would
        # tear down the whole event loop before we can inspect the failure.
        # Wrapping it lets us translate to a normal exception that ``.exception()``
        # surfaces in the polling loop below.
        server = self._server

        async def _serve_safely():
            try:
                await server.serve()
            except SystemExit as e:
                raise RuntimeError(f"uvicorn exited with code {e.code}") from e

        self._task = asyncio.create_task(_serve_safely())

        # Wait for uvicorn to flip `started=True` (it does this once it's bound
        # the socket and entered the accept loop). Polling every 50 ms keeps
        # the happy path responsive while still surfacing bind failures fast.
        deadline = loop.time() + 5.0
        while loop.time() < deadline:
            if self._task.done():
                # Task ended before the server became ready — re-raise the
                # underlying exception so initialize() can fail loudly.
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

        # Soft timeout — log loudly but don't kill the plugin; the server may
        # still come up shortly. The next failed request will be the real signal.
        logger.warning(
            f"KiraOS WebUI did not report ready within 5s on {self.host}:{self.port}; "
            "continuing anyway"
        )

    def _cleanup_log_handlers(self):
        """Detach the access-log filter and restore the asyncio exception handler."""
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

        self._cleanup_log_handlers()
        logger.info("KiraOS Memory WebUI stopped")
