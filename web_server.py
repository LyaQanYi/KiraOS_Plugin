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
    _SAFE_ID_RE,
    ENTITY_USER,
    VALID_ENTITY_TYPES,
    MEMORY_FOLDERS,
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


def _validate_entity_id(entity_id: str) -> bool:
    """Return True iff `entity_id` matches the same safe-ID regex used by
    `memory_paths._validate_id`. Lets handlers reject bad path params as 400
    instead of letting the deeper `get_entity_dir`/`get_memory` blow up as
    500.
    """
    return bool(entity_id) and bool(_SAFE_ID_RE.match(entity_id))


def _validate_folder(folder: str) -> bool:
    """Return True iff `folder` is one of MEMORY_FOLDERS."""
    return folder in MEMORY_FOLDERS


def _resolve_path_params(
    request: Request,
    *,
    require_folder: bool = False,
) -> tuple[Optional[JSONResponse], Optional[str], Optional[str], Optional[str]]:
    """Unified path-param validator for handlers that take
    `/entity/{entity_type}/{entity_id}/...` (optionally `/{folder}`).

    Returns `(error_response, entity_type, entity_id, folder)`:
    - On success, `error_response` is None and the other three are normalized.
    - On any validation failure, `error_response` is a 400 JSONResponse with
      a short, deterministic error string suitable for the SPA, and the
      other three are None.

    This collapses the previous pattern of `_validate_entity_type` (collapse
    to ENTITY_USER) + `get_entity_dir(...)` raising deep ValueError into a
    single front-door check, so handlers don't return 500 for bad params.
    """
    entity_type = _validate_entity_type(request.path_params.get("entity_type", ""))
    if entity_type is None:
        return JSONResponse({"error": "invalid entity_type"}, status_code=400), None, None, None

    entity_id = request.path_params.get("entity_id", "")
    if not _validate_entity_id(entity_id):
        return JSONResponse({"error": "invalid entity_id"}, status_code=400), None, None, None

    folder = request.path_params.get("folder")
    if require_folder:
        if not folder or not _validate_folder(folder):
            return JSONResponse({"error": "invalid folder"}, status_code=400), None, None, None
    elif folder is not None and not _validate_folder(folder):
        return JSONResponse({"error": "invalid folder"}, status_code=400), None, None, None

    return None, entity_type, entity_id, folder


def _memory_to_dict(mem) -> dict:
    """Serialize a Memory object to a JSON-safe dict for the SPA.

    Intentionally omits `file_path` — exposing the on-disk layout to the
    frontend is a stable information-leak surface (data-root, namespace
    structure) the SPA doesn't need. Server-side debugging can read it
    from logs instead.
    """
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

    err, entity_type, entity_id, _ = _resolve_path_params(request)
    if err is not None:
        return err

    try:
        profile = await manager.profile_store.get_profile(entity_id, entity_type)
    except Exception:
        logger.exception("get_entity profile load failed for %s/%s", entity_type, _mask_id(entity_id))
        return JSONResponse({"error": "internal error"}, status_code=500)

    # 走 `tree_store.get_all_memories` 而不是直接读 SQLite 索引——TOML 才是
    # 真相源，索引行可能有用户手动编辑后没同步的旧值。`_memory_to_dict` 做受
    # 控序列化，把 `file_path` 之类的内部字段过滤掉。
    facts = [
        _memory_to_dict(m)
        for m in await manager.tree_store.get_all_memories(
            entity_id=entity_id, entity_type=entity_type, folder="facts"
        )
    ]
    reflections = [
        _memory_to_dict(m)
        for m in await manager.tree_store.get_all_memories(
            entity_id=entity_id, entity_type=entity_type, folder="reflections"
        )
    ]

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


async def api_evidence_timeline(request: Request) -> JSONResponse:
    """GET /api/evidence/timeline/{kind}/{target_id} — signal history.

    Returns the applied-signal log + the current ledger snapshot in
    one round-trip so the WebUI can render both the per-event strip
    and the current rein/disp state without two requests.
    """
    db = _get_db(request)
    if not db:
        return JSONResponse({"error": "Database not available"}, status_code=503)
    kind = request.path_params.get("kind", "")
    target_id = request.path_params.get("target_id", "")
    if kind not in ("profile", "reflection", "fact"):
        return JSONResponse(
            {"error": f"invalid target_kind: {kind}"}, status_code=400,
        )
    try:
        limit = int(request.query_params.get("limit", "200"))
    except (TypeError, ValueError):
        limit = 200
    limit = max(1, min(2000, limit))
    try:
        history = db.list_evidence_history(kind, target_id, limit=limit)
        snapshot = db.get_evidence_snapshot(kind, target_id)
        return JSONResponse({
            "target_kind": kind,
            "target_id": target_id,
            "snapshot": snapshot,
            "history": history,
        })
    except Exception:
        logger.exception("Error fetching timeline for %s/%s", kind, target_id)
        return JSONResponse({"error": "Internal server error"}, status_code=500)


async def api_user_entities(request: Request) -> JSONResponse:
    """GET /api/users/{user_id}/entities — entity aggregation.

    Returns ``{user_id, entities: [{entity, count, profiles: [...]}]}``.
    Used by the entity-graph view to lay out nodes + edges.
    """
    db = _get_db(request)
    if not db:
        return JSONResponse({"error": "Database not available"}, status_code=503)
    user_id = request.path_params["user_id"]
    try:
        ents = db.list_entities(user_id)
        return JSONResponse({"user_id": user_id, "entities": ents})
    except Exception:
        logger.exception("Error listing entities for %s", _mask_id(user_id))
        return JSONResponse({"error": "Internal server error"}, status_code=500)


async def api_evidence_preview(request: Request) -> JSONResponse:
    """POST /api/evidence/preview — what-if half-life calculator.

    JSON body: ``{target_kind, target_id, rein_half_life_days,
    disp_half_life_days}``. Returns the would-be effective rein/disp
    + net score. Read-only — never touches the ledger.

    Useful for the WebUI Half-life Playground: drag the slider, send
    a preview request per target, render the new score next to the
    stored one. Targets are evaluated independently so the response
    is a single object per call.
    """
    db = _get_db(request)
    if not db:
        return JSONResponse({"error": "Database not available"}, status_code=503)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "body must be an object"}, status_code=400)
    kind = body.get("target_kind", "")
    if kind not in ("profile", "reflection", "fact"):
        return JSONResponse(
            {"error": f"invalid target_kind: {kind!r}"}, status_code=400,
        )
    target_id = str(body.get("target_id", "") or "")
    if not target_id:
        return JSONResponse({"error": "target_id required"}, status_code=400)
    try:
        rhl = float(body.get("rein_half_life_days", 14.0))
        dhl = float(body.get("disp_half_life_days", 7.0))
    except (TypeError, ValueError):
        return JSONResponse(
            {"error": "half_life_days fields must be numeric"}, status_code=400,
        )
    try:
        result = db.preview_evidence_score(
            kind, target_id,
            rein_half_life_days=rhl,
            disp_half_life_days=dhl,
        )
        return JSONResponse(result)
    except Exception:
        logger.exception("Error in evidence preview")
        return JSONResponse({"error": "Internal server error"}, status_code=500)


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


async def api_update_profile(request: Request) -> JSONResponse:
    manager = _get_manager(request)
    if not manager:
        return JSONResponse({"error": "memory not ready"}, status_code=503)

    err, entity_type, entity_id, _ = _resolve_path_params(request)
    if err is not None:
        return err

    try:
        payload = await request.json()
    except json.JSONDecodeError:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse(
            {"error": "JSON body must be an object"}, status_code=400
        )

    allowed = {"name", "nickname", "description", "platform"}
    updates = {k: v for k, v in payload.items() if k in allowed and isinstance(v, str)}
    if not updates:
        return JSONResponse({"error": "no valid fields"}, status_code=400)

    try:
        await manager.profile_store.update_profile(entity_id, entity_type, **updates)
    except Exception:
        logger.exception("update_profile failed for %s/%s", entity_type, _mask_id(entity_id))
        return JSONResponse({"error": "internal error"}, status_code=500)

    return JSONResponse({"ok": True})


async def api_add_fact(request: Request) -> JSONResponse:
    manager = _get_manager(request)
    if not manager:
        return JSONResponse({"error": "memory not ready"}, status_code=503)

    err, entity_type, entity_id, _ = _resolve_path_params(request)
    if err is not None:
        return err

    try:
        payload = await request.json()
    except json.JSONDecodeError:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse(
            {"error": "JSON body must be an object"}, status_code=400
        )

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
    except Exception:
        logger.exception("add_fact failed for %s/%s", entity_type, _mask_id(entity_id))
        return JSONResponse({"error": "internal error"}, status_code=500)

    return JSONResponse({"ok": True, "memory_id": mem.id})


async def api_delete_entity(request: Request) -> JSONResponse:
    manager = _get_manager(request)
    if not manager:
        return JSONResponse({"error": "memory not ready"}, status_code=503)

    err, entity_type, entity_id, _ = _resolve_path_params(request)
    if err is not None:
        return err

    base_dir = get_entity_dir(entity_id, entity_type)
    if not os.path.isdir(base_dir):
        return JSONResponse({"error": "entity not found"}, status_code=404)

    # Move the whole entity dir into archive/ to keep raw data recoverable.
    # Order matters: clear index rows FIRST so a crash between the two steps
    # leaves at worst orphan archived files (recoverable) rather than stale
    # index rows pointing into a moved-away directory (would surface as ghost
    # search results that then fail to open).
    #
    # The whole thing is offloaded to `asyncio.to_thread` so the sync SQLite
    # work + (potentially cross-filesystem) `shutil.move` doesn't stall the
    # event loop. With many entities or large dirs this used to make every
    # other API jitter for the duration of the delete.
    def _do_delete_entity_sync():
        # 1. Drop index rows for this entity so search/recall stop surfacing
        #    them. `MemoryIndex.delete` 按复合主键 (entity_type, entity_id,
        #    folder, base_dir, id) 删单行；row 自身就携带完整 entity 维度，
        #    直接转发。
        for folder in ("facts", "reflections"):
            for row in manager.memory_index.list_memories(
                entity_id=entity_id, entity_type=entity_type, folder=folder
            ):
                manager.memory_index.delete(
                    row.get("id"),
                    entity_id=row.get("entity_id", entity_id),
                    entity_type=row.get("entity_type", entity_type),
                    folder=row.get("folder", folder),
                    base_dir=row.get("base_dir", ""),
                )
        # 2. Move the on-disk entity dir into archive. Wall-clock `time.time()`
        #    intentionally, not `asyncio.get_event_loop().time()` (monotonic).
        archive_root = Path(get_entities_dir()).parent / "archive" / "_full_entities"
        archive_root.mkdir(parents=True, exist_ok=True)
        target = archive_root / f"{entity_type}_{_id_to_path_segment(entity_id)}_{int(time.time())}"
        shutil.move(base_dir, target)

    try:
        await asyncio.to_thread(_do_delete_entity_sync)
    except Exception:
        logger.exception("delete_entity failed for %s/%s", entity_type, _mask_id(entity_id))
        return JSONResponse({"error": "internal error"}, status_code=500)

    return JSONResponse({"ok": True})


async def api_update_memory(request: Request) -> JSONResponse:
    manager = _get_manager(request)
    if not manager:
        return JSONResponse({"error": "memory not ready"}, status_code=503)

    err, entity_type, entity_id, folder = _resolve_path_params(request, require_folder=True)
    if err is not None:
        return err
    memory_id = request.path_params["memory_id"]
    # memory_id 会拼进 os.path.join(dir, f"{memory_id}.toml")，必须先走和
    # entity_id 同等的 safe-id 校验，否则一个含 `/` / `\` 的 segment 就能
    # 越出目标目录读到别处。
    if not _validate_entity_id(memory_id):
        return JSONResponse({"error": "invalid memory_id"}, status_code=400)

    try:
        payload = await request.json()
    except json.JSONDecodeError:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse(
            {"error": "JSON body must be an object"}, status_code=400
        )

    memory = await manager.tree_store.get_memory(
        memory_id=memory_id,
        entity_id=entity_id,
        entity_type=entity_type,
        folder=folder,
    )
    if not memory:
        return JSONResponse({"error": "memory not found"}, status_code=404)

    # 编辑接口里凡是显式提供了字段但类型不对，都直接报 400 而不是静默忽略。
    # 之前的 silent-drop 会让前端误以为修改已落地，实际磁盘没动。
    if "text" in payload:
        if not isinstance(payload["text"], str):
            return JSONResponse({"error": "text must be a string"}, status_code=400)
        if len(payload["text"]) > MAX_TEXT_LEN:
            return JSONResponse({"error": f"text too long (>{MAX_TEXT_LEN})"}, status_code=400)
        memory.text = payload["text"]
    if "importance" in payload:
        try:
            memory.importance = max(1, min(10, int(payload["importance"])))
        except (TypeError, ValueError):
            return JSONResponse(
                {"error": "importance must be an integer 1-10"}, status_code=400
            )
    if "tags" in payload:
        if not isinstance(payload["tags"], list):
            return JSONResponse({"error": "tags must be a list"}, status_code=400)
        memory.tags = [str(t) for t in payload["tags"]]

    ok = await manager.tree_store.update_memory(memory)
    if not ok:
        # 把持久化失败映射成 5xx，避免前端 / SDK 以 2xx 判定成功而误以为
        # 修改已落地。日志里有具体异常上下文（tree_store.update_memory 会
        # logger.error）；body 不暴露底层细节。
        logger.warning(
            "update_memory returned False for %s/%s/%s/%s",
            entity_type,
            _mask_id(entity_id),
            folder,
            memory_id,
        )
        return JSONResponse({"error": "update failed"}, status_code=500)
    return JSONResponse({"ok": True})


async def api_delete_memory(request: Request) -> JSONResponse:
    manager = _get_manager(request)
    if not manager:
        return JSONResponse({"error": "memory not ready"}, status_code=503)

    err, entity_type, entity_id, folder = _resolve_path_params(request, require_folder=True)
    if err is not None:
        return err
    memory_id = request.path_params["memory_id"]
    # memory_id 会拼进文件路径，同样必须做 safe-id 校验防路径逃逸
    if not _validate_entity_id(memory_id):
        return JSONResponse({"error": "invalid memory_id"}, status_code=400)

    # 先用 get_memory 探一下记录是否真的存在——`archive_memory` 的 False
    # 同时表示"目标不存在"和"写归档/删索引失败"，把两者都报 404 会让真实
    # 的 5xx 故障被前端 / SDK 误当成 not found。
    existing = await manager.tree_store.get_memory(
        memory_id=memory_id,
        entity_id=entity_id,
        entity_type=entity_type,
        folder=folder,
    )
    if not existing:
        return JSONResponse({"error": "memory not found"}, status_code=404)

    ok = await manager.tree_store.archive_memory(
        memory_id=memory_id,
        entity_id=entity_id,
        entity_type=entity_type,
        folder=folder,
    )
    if not ok:
        # 文件刚才还在但 archive 失败——是真实的写归档 / 删索引错误。
        logger.warning(
            "archive_memory returned False after get_memory hit for %s/%s/%s/%s",
            entity_type,
            _mask_id(entity_id),
            folder,
            memory_id,
        )
        return JSONResponse({"error": "archive failed"}, status_code=500)
    return JSONResponse({"ok": True})


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
    # known entity so the user can do a true global recall.
    # - **Concurrent**: `asyncio.gather` the per-entity `recall` calls instead
    #   of running them sequentially — that turns O(N) latency back into the
    #   slowest single call.
    # - **Score-preserving sort**: each Memory's `meta["_score"]` carries the
    #   hybrid BM25 + vector + importance + decay score from `MemoryIndex.
    #   hybrid_search`. Merging by `_score` keeps cross-entity relevance
    #   ordering intact; falling back to `(importance, last_accessed)` only
    #   when the score is missing (e.g. a memory that never went through the
    #   FTS path).
    if not entity_id and not entity_type:
        entities = list_all_entities()

        async def _recall_one(eid: str, etype: str):
            try:
                return await manager.recall(
                    query=query, entity_id=eid, entity_type=etype, k=k
                )
            except Exception as e:
                logger.warning(
                    "global recall failed for %s:%s: %s",
                    etype,
                    _mask_id(eid),
                    e,
                )
                return []

        per_entity = await asyncio.gather(
            *(_recall_one(eid, etype) for eid, etype in entities)
        )
        results = [m for hits in per_entity for m in hits]

        def _sort_key(m):
            meta = getattr(m, "meta", None) or {}
            return (
                float(meta.get("_score") or 0.0),
                int(m.importance or 0),
                float(m.last_accessed or 0),
            )

        results.sort(key=_sort_key, reverse=True)
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
        Route("/api/users/{user_id}/profiles/{key}", api_update_profile, methods=["PUT"]),
        Route("/api/users/{user_id}/profiles/{key}", api_delete_profile, methods=["DELETE"]),
        Route("/api/users/{user_id}/events/{event_id:int}", api_update_event, methods=["PUT"]),
        Route("/api/users/{user_id}/events/{event_id:int}", api_delete_event, methods=["DELETE"]),
        Route("/api/users/{user_id}/events", api_add_event, methods=["POST"]),
        Route("/api/users/{user_id}/recall", api_user_recall, methods=["GET"]),
        Route("/api/users/{user_id}/entities", api_user_entities, methods=["GET"]),
        Route("/api/users/{user_id}/reflections", api_list_reflections, methods=["GET"]),
        Route("/api/users/{user_id}/reflections/{rid}/promote", api_promote_reflection, methods=["POST"]),
        Route("/api/users/{user_id}/reflections/{rid}/deny", api_deny_reflection, methods=["POST"]),
        Route("/api/users/{user_id}", api_get_user, methods=["GET"]),
        Route("/api/users/{user_id}", api_clear_user, methods=["DELETE"]),
        Route("/api/export", api_export, methods=["GET"]),
        Route("/api/import", api_import, methods=["POST"]),
        Route("/api/embeddings/backfill", api_embeddings_backfill, methods=["POST"]),
        Route("/api/evidence/timeline/{kind}/{target_id}", api_evidence_timeline, methods=["GET"]),
        Route("/api/evidence/preview", api_evidence_preview, methods=["POST"]),
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
