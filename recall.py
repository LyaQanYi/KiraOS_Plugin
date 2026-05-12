"""Memory recall pipeline for KiraOS_Plugin Phase 2.

Stages, executed in order. Each stage's input is the previous stage's
output; each stage is independently switchable and *each one can be
absent without breaking the pipeline*:

  Stage A — FTS5 BM25 over event_logs (db.py:search_events_fts).
            Cheap, always-on when FTS5 is built. Returns ``budget * 3``
            candidates by default so the next stages have headroom.
            Falls back to LIKE for sub-trigram queries internally.

  Stage B — Optional semantic re-rank by cosine similarity against the
            query's embedding. Only the candidates whose ``embedding``
            column is populated participate; missing-embedding rows
            keep their Stage-A rank. No-op when EmbeddingService is
            disabled or returns None for the query.

  Stage C — Optional LLM rerank. Sends candidate summaries to a fast
            LLM with a short rerank prompt; the LLM returns a permutation
            of the candidate indices. On any failure (timeout, malformed
            response, no client) Stage B's order is preserved.

The recaller never raises. A pipeline error degrades to whatever the
last-good stage produced — at minimum, an empty list.
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Optional

from core.logging_manager import get_logger

from .embeddings import (
    EmbeddingService,
    cosine_similarity,
    decode_vector,
)

logger = get_logger("kiraos_recall", "green")


# Stage-C prompt — kept short so the rerank LLM call stays under 1 KB.
# Format: 一行一条候选，行首数字索引，让模型回 JSON list 同样的索引集合。
_RERANK_PROMPT_TEMPLATE = (
    "你是一个记忆检索的精排助手。给定一个查询和若干候选事件，"
    "返回最相关的若干条索引（从 0 开始），按相关度从高到低。"
    "只输出形如 [3, 1, 0] 的 JSON 数组，不要任何解释。\n\n"
    "查询: {query}\n\n候选:\n{candidates}\n\n返回前 {k} 条索引:"
)


@dataclass
class RecallCandidate:
    """A single candidate row threaded through all three stages.

    Stages mutate the score/stage_scores in place so the pipeline output
    can carry full provenance back to the caller (e.g. the WebUI
    /api/recall endpoint surfaces this for debugging).
    """
    event_id: int
    summary: str
    created_at: str
    tag: Optional[str]
    # Running aggregate score (post-stage). Bigger is better; comparable
    # only within one pipeline invocation, not across queries.
    score: float
    # Per-stage scores for telemetry; absent stages stay missing.
    stage_scores: dict = field(default_factory=dict)


@dataclass
class RecallConfig:
    """Resolved per-call recall settings. The plugin layer fills this
    from cfg + the LLM client / embedding service at request time so
    individual recall() calls can override globals if needed.
    """
    enable_embedding: bool = False
    enable_llm_rerank: bool = False
    # How many candidates to ask the DB for. We over-fetch on purpose
    # so embedding cosine + LLM rerank have headroom — without this,
    # truncation at the DB layer would silently cap recall quality.
    fts_overfetch_multiplier: int = 3
    # Per-stage LLM rerank ceiling. The full prompt scales linearly with
    # candidates; 20 keeps the prompt under ~1 KB.
    llm_rerank_max_candidates: int = 20
    # Hard timeout for the rerank LLM call. Five seconds is plenty for
    # a fast model and well below typical chat-loop budgets; on timeout
    # we keep Stage B's order silently.
    llm_rerank_timeout_s: float = 5.0


class MemoryRecaller:
    """Three-stage recall over a single user's event log.

    The plugin constructs one of these alongside the DB and reuses it
    for every recall call — there's no per-request state beyond the
    config snapshot, so it's cheap to keep around.
    """

    def __init__(
        self,
        db,  # UserMemoryDB; typed loosely to avoid import cycle
        *,
        embedding_service: Optional[EmbeddingService] = None,
        llm_client_provider=None,
        config: Optional[RecallConfig] = None,
    ):
        """
        Args:
            db: the plugin's UserMemoryDB instance. Must already have
                schema migrations applied (Phase 1) — this class assumes
                ``search_events_fts`` and the embedding columns exist.
            embedding_service: optional. If omitted, Stage B is a no-op.
            llm_client_provider: 0-arg callable returning a fast LLM
                client with an ``a_complete(prompt, ...)`` /
                ``complete(prompt, ...)`` method. If omitted, Stage C
                is a no-op.
            config: defaults to a fresh RecallConfig().
        """
        self.db = db
        self.embedding_service = embedding_service
        self._llm_client_provider = llm_client_provider
        self.config = config or RecallConfig()

    async def recall(
        self,
        user_id: str,
        query: str,
        *,
        budget: int = 10,
    ) -> list[RecallCandidate]:
        """Run the full three-stage pipeline.

        Args:
            user_id: scope of the search; the DB layer enforces this.
            query: free-text query from the user. Empty queries return
                an empty list immediately (no implicit "list all").
            budget: how many candidates the caller wants returned. The
                pipeline over-fetches at Stage A so re-ranking has room.

        Returns ``budget`` (or fewer) RecallCandidates sorted best-first.
        """
        q = (query or "").strip()
        if not q or budget <= 0:
            return []

        # Stage A — FTS5 (with internal LIKE fallback for short queries).
        overfetch = max(budget, budget * self.config.fts_overfetch_multiplier)
        try:
            rows = self.db.search_events_fts(user_id, q, limit=overfetch)
        except Exception as exc:
            logger.warning(f"Stage-A FTS search failed: {exc}; recall returning []")
            return []
        if not rows:
            return []
        candidates: list[RecallCandidate] = []
        for event_id, summary, created_at, tag, score in rows:
            cand = RecallCandidate(
                event_id=event_id,
                summary=summary,
                created_at=created_at,
                tag=tag,
                score=float(score),
                stage_scores={"fts": float(score)},
            )
            candidates.append(cand)

        # Stage B — embedding cosine (optional)
        if self.config.enable_embedding and self.embedding_service is not None:
            candidates = await self._rerank_by_embedding(q, candidates)

        # Stage C — LLM rerank (optional)
        if self.config.enable_llm_rerank and self._llm_client_provider is not None:
            top_for_llm = candidates[: self.config.llm_rerank_max_candidates]
            reranked = await self._rerank_by_llm(q, top_for_llm)
            if reranked is not None:
                # Replace the head of the candidate list with the
                # reranked subset and keep any tail unchanged.
                tail = candidates[self.config.llm_rerank_max_candidates:]
                candidates = reranked + tail

        return candidates[:budget]

    async def _rerank_by_embedding(
        self,
        query: str,
        candidates: list[RecallCandidate],
    ) -> list[RecallCandidate]:
        """Pull each candidate's stored embedding from the DB, cosine
        against the query embedding, and blend with Stage-A score.

        Candidates missing an embedding (e.g. legacy rows from before
        Phase 4 backfill) keep their Stage-A score unchanged — they
        aren't penalized, just not boosted. This makes embedding a
        "best-effort enhancement" rather than a hard filter.

        Blending: ``final = 0.4 * fts + 0.6 * cosine``. Weights chosen
        empirically: cosine carries more signal for paraphrase / topic
        queries (Phase 2's main motivation) but BM25 still grounds the
        ranking when the cosine model is weak or the query is a
        verbatim quote.
        """
        assert self.embedding_service is not None
        q_blob = await self.embedding_service.aembed(query)
        q_vec = decode_vector(q_blob) if q_blob is not None else None
        if q_vec is None:
            # Embedding backend disabled or query couldn't be encoded
            # (provider returned None / 0-length). Silently skip Stage B.
            return candidates

        # Pull all candidate embeddings in one round-trip.
        ids = [c.event_id for c in candidates]
        if not ids:
            return candidates
        placeholders = ",".join("?" * len(ids))
        conn = self.db._get_conn()
        rows = conn.execute(
            f"SELECT id, embedding FROM event_logs WHERE id IN ({placeholders})",
            ids,
        ).fetchall()
        emb_by_id = {row[0]: row[1] for row in rows}

        # Track raw cosine for telemetry but also compute a blended
        # final score. Normalize FTS score into [0,1] using the local
        # batch's max so blending is well-defined regardless of the
        # absolute BM25 magnitudes (which depend on corpus size).
        max_fts = max((c.score for c in candidates), default=1.0) or 1.0
        for cand in candidates:
            cos = 0.0
            vec = decode_vector(emb_by_id.get(cand.event_id))
            if vec is not None and len(vec) == len(q_vec):
                cos = cosine_similarity(q_vec, vec)
            cand.stage_scores["cosine"] = cos
            normalized_fts = cand.score / max_fts
            cand.score = 0.4 * normalized_fts + 0.6 * cos

        candidates.sort(key=lambda c: c.score, reverse=True)
        return candidates

    async def _rerank_by_llm(
        self,
        query: str,
        candidates: list[RecallCandidate],
    ) -> Optional[list[RecallCandidate]]:
        """Call a fast LLM to permute the top-N candidates by relevance.

        Returns the reordered list, or ``None`` to signal "keep previous
        order" (timeout / malformed response / no client).
        """
        if not candidates:
            return candidates
        try:
            client = self._llm_client_provider()
        except Exception as exc:
            logger.debug(f"rerank LLM provider raised: {exc}; skipping Stage C")
            return None
        if client is None:
            return None

        # Build the prompt body — number each candidate so the LLM only
        # has to return short integers. Truncate summaries so long
        # entries don't blow the prompt budget.
        lines = []
        for idx, cand in enumerate(candidates):
            summary = cand.summary
            if len(summary) > 160:
                summary = summary[:159] + "…"
            lines.append(f"{idx}. {summary}")
        prompt = _RERANK_PROMPT_TEMPLATE.format(
            query=query,
            candidates="\n".join(lines),
            k=len(candidates),
        )

        # Resolve the call — async preferred, sync fallback.
        text = None
        try:
            call = getattr(client, "a_complete", None) or getattr(client, "acomplete", None)
            if call is not None:
                text = await asyncio.wait_for(
                    call(prompt), timeout=self.config.llm_rerank_timeout_s
                )
            else:
                sync_call = getattr(client, "complete", None)
                if sync_call is None:
                    return None
                text = await asyncio.wait_for(
                    asyncio.to_thread(sync_call, prompt),
                    timeout=self.config.llm_rerank_timeout_s,
                )
        except asyncio.TimeoutError:
            logger.debug("Stage-C LLM rerank timed out; keeping Stage-B order")
            return None
        except Exception as exc:
            logger.debug(f"Stage-C LLM rerank failed ({exc}); keeping Stage-B order")
            return None

        if not text:
            return None
        # Parse the first JSON-array-shaped substring; the LLM might wrap
        # it in code fences or stray prose despite the instruction.
        match = re.search(r"\[[^\[\]]*\]", text)
        if not match:
            return None
        try:
            import json
            order = json.loads(match.group(0))
        except Exception:
            return None
        if not isinstance(order, list):
            return None

        # Build the reordered list, dropping invalid indices and dedup'ing
        # so a hallucinated "0, 0, 1" doesn't return duplicates.
        seen: set[int] = set()
        reordered: list[RecallCandidate] = []
        for raw in order:
            try:
                idx = int(raw)
            except (TypeError, ValueError):
                continue
            if 0 <= idx < len(candidates) and idx not in seen:
                seen.add(idx)
                cand = candidates[idx]
                # Score: descending integers so the new order survives
                # any later sort. Stage telemetry preserved.
                cand.stage_scores["llm_rank"] = len(candidates) - len(reordered)
                cand.score = float(len(candidates) - len(reordered))
                reordered.append(cand)
        # Append any candidates the LLM dropped, in their previous order,
        # so we don't *lose* relevant rows just because the LLM missed
        # them. They sort below the reranked head naturally.
        for idx, cand in enumerate(candidates):
            if idx not in seen:
                cand.stage_scores["llm_rank"] = 0
                reordered.append(cand)
        return reordered
