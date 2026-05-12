"""Optional embedding service for KiraOS_Plugin Phase 2 recall.

Embeddings are a strictly opt-in layer: the plugin works end-to-end
without them via FTS5 + LIKE fallback (see ``recall.py``). When enabled,
this module supplies vectors that the recaller's middle stage uses to
re-rank an FTS shortlist by semantic similarity.

Backend selection priority:

  1. The plugin host's default LLM client, if it exposes an
     ``aembed(text) -> list[float]`` / ``embed(text) -> list[float]``
     method (most provider SDKs do).
  2. ``sentence-transformers`` if the user has installed it locally
     (no auto-install; we never touch their venv).
  3. Disabled — the service is permanently inactive and every embed()
     call returns ``None``. Recaller skips the cosine stage entirely.

The fp16 base64 BLOB encoding is shared with NEKO so a future
backfill across the two systems would be possible without recomputing.
``compute_text_sha`` lets callers cheaply detect "the value changed but
the embedding column is stale" without decoding the BLOB.
"""
from __future__ import annotations

import base64
import hashlib
import math
import struct
from typing import Optional, Sequence

from core.logging_manager import get_logger

logger = get_logger("kiraos_embeddings", "green")


# ── BLOB codec (fp16 base64, NEKO-compatible) ──────────────────────

def encode_vector(vec: Sequence[float]) -> bytes:
    """Pack a float vector into the fp16-base64 BLOB stored in SQLite.

    Format: ``<dim:uint16-LE><dim × float16-LE>`` then base64-encoded.
    fp16 trades 4× space for ~0.001 cosine error vs fp32 — empirically
    indistinguishable for short-text similarity ranking, but cuts a
    1536-dim vector from 6 KB to 1.5 KB per row.
    """
    if not vec:
        raise ValueError("embedding vector is empty")
    dim = len(vec)
    if dim > 0xFFFF:
        raise ValueError(f"embedding dim {dim} exceeds uint16")
    buf = bytearray(struct.pack("<H", dim))
    for v in vec:
        # struct's 'e' is IEEE 754 binary16 (fp16). It's been in stdlib
        # since Python 3.6 and handles NaN/inf correctly.
        buf.extend(struct.pack("<e", float(v)))
    return base64.b64encode(bytes(buf))


def decode_vector(blob: Optional[bytes]) -> Optional[list[float]]:
    """Inverse of ``encode_vector``. Returns None on any decode failure
    so callers can transparently treat "missing" and "corrupt" the same.
    """
    if not blob:
        return None
    try:
        raw = base64.b64decode(blob)
        dim = struct.unpack_from("<H", raw, 0)[0]
        if dim == 0 or 2 + dim * 2 > len(raw):
            return None
        return [
            struct.unpack_from("<e", raw, 2 + i * 2)[0]
            for i in range(dim)
        ]
    except (struct.error, ValueError, TypeError):
        return None


def compute_text_sha(text: str, model_tag: str = "") -> str:
    """SHA-256 of ``model_tag\\x00text`` — stored alongside the BLOB so
    callers can ask "is the cached embedding still valid for this text
    and this model?" without decoding the vector.

    Putting the model tag into the hash means swapping embedding models
    automatically invalidates every cached vector (a stale 1536-dim
    OpenAI vector is useless if we're now generating 384-dim MiniLM
    vectors).
    """
    h = hashlib.sha256()
    h.update(model_tag.encode("utf-8"))
    h.update(b"\x00")
    h.update((text or "").encode("utf-8", errors="replace"))
    return h.hexdigest()


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    """Plain cosine for two equal-length vectors. Returns 0.0 on
    length mismatch or zero-norm (well-formed but uninformative inputs).
    """
    if len(a) != len(b) or not a:
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0 or nb <= 0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


# ── Service ────────────────────────────────────────────────────────

class EmbeddingService:
    """Lazy, fail-soft embedding provider.

    All public methods are guaranteed not to raise — a misconfigured or
    missing backend surfaces as ``is_available() == False`` and
    ``embed()`` returning ``None``. The recaller treats those as "skip
    the cosine stage", so a broken embedding setup never blocks search.

    Backends are resolved on first ``embed()`` call (not at construction)
    so the plugin's ``initialize()`` cost stays small even when this
    service is left enabled but unused.
    """

    # Sentinel for "haven't tried yet"
    _UNRESOLVED = object()

    def __init__(
        self,
        *,
        enabled: bool = False,
        llm_client_provider=None,
        model_tag: str = "default",
    ):
        """
        Args:
            enabled: master switch. ``False`` (default) → every call is
                a no-op; backend resolution is skipped entirely.
            llm_client_provider: a 0-arg callable returning an LLM client
                with an embed/aembed method, or ``None``. Used as the
                preferred backend. The plugin passes
                ``ctx.get_default_llm_client``.
            model_tag: opaque string that goes into ``compute_text_sha``.
                Bump it when you intentionally swap models so old
                cached vectors are recomputed on next read.
        """
        self._enabled = bool(enabled)
        self._llm_client_provider = llm_client_provider
        self._model_tag = model_tag or "default"
        # Resolved backend: None (unresolved sentinel), False (resolved
        # but unavailable — don't retry), or a callable text→vec.
        self._backend: object = self._UNRESOLVED if enabled else False

    @property
    def model_tag(self) -> str:
        return self._model_tag

    def is_available(self) -> bool:
        """True iff a backend is wired and the service is enabled.

        Calling this triggers backend resolution on first use, so the
        caller can fail-fast if it wants to log a "embedding off" notice.
        """
        if not self._enabled:
            return False
        if self._backend is self._UNRESOLVED:
            self._resolve_backend()
        return callable(self._backend)

    def embed(self, text: str) -> Optional[bytes]:
        """Encode ``text`` → fp16 BLOB. Returns ``None`` when disabled,
        when no backend is available, or on backend error.

        Synchronous; if the underlying backend is async, prefer
        ``aembed`` from an async context.
        """
        if not self.is_available():
            return None
        try:
            vec = self._backend(text)  # type: ignore[misc]
            if not vec:
                return None
            return encode_vector(vec)
        except Exception as exc:
            logger.warning(f"embed() backend failure: {exc}; disabling")
            self._backend = False
            return None

    async def aembed(self, text: str) -> Optional[bytes]:
        """Async version. Falls back to sync if backend isn't a coroutine."""
        if not self.is_available():
            return None
        try:
            result = self._backend(text)  # type: ignore[misc]
            if hasattr(result, "__await__"):
                vec = await result
            else:
                vec = result
            if not vec:
                return None
            return encode_vector(vec)
        except Exception as exc:
            logger.warning(f"aembed() backend failure: {exc}; disabling")
            self._backend = False
            return None

    def _resolve_backend(self):
        """Try the LLM-client embed endpoint first, then
        sentence-transformers, then disable.

        We never raise from here: failure to resolve == disabled. The
        caller (is_available / embed) then gracefully no-ops.
        """
        # 1) Provider-supplied LLM client
        if self._llm_client_provider is not None:
            try:
                client = self._llm_client_provider()
            except Exception as exc:
                logger.debug(f"embedding: llm_client_provider raised {exc}")
                client = None
            if client is not None:
                fn = None
                for attr in ("aembed", "embed", "embed_text"):
                    candidate = getattr(client, attr, None)
                    if callable(candidate):
                        fn = candidate
                        break
                if fn is not None:
                    self._backend = fn
                    self._model_tag = getattr(client, "model", self._model_tag)
                    logger.info(
                        f"EmbeddingService: using LLM-client backend "
                        f"({self._model_tag})"
                    )
                    return
        # 2) Local sentence-transformers (only if installed)
        try:
            from sentence_transformers import SentenceTransformer  # type: ignore
            # Lightweight default; user can override by passing a custom
            # provider that returns a different model instance.
            model = SentenceTransformer(
                "paraphrase-multilingual-MiniLM-L12-v2"
            )
            self._backend = lambda text, _m=model: _m.encode(
                text or "", convert_to_numpy=False
            ).tolist()
            self._model_tag = "sentence-transformers/MiniLM-L12-v2"
            logger.info("EmbeddingService: using sentence-transformers backend")
            return
        except ImportError:
            pass
        except Exception as exc:
            # Model download / load failure — disable; user can fix env
            # and retry by restarting the plugin.
            logger.warning(
                f"sentence-transformers load failed ({exc}); embedding disabled"
            )
        # 3) Disabled — no backend resolved
        self._backend = False
        logger.info(
            "EmbeddingService: no backend available (this is fine — "
            "recaller will run FTS-only)"
        )
