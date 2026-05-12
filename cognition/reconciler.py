"""Three-stage cognition reconciler (Phase 3b).

Stages mirror NEKO's facts → reflections → persona cycle, but bound
to our existing tables:

  Stage 1 — atomic fact persistence (already happens in
            ``memory_update(op='event')`` via Phase 3a's fact_hash
            dedup + rein signal). This module doesn't redo Stage 1;
            it consumes its output.

  Stage 2 — ``synthesize_if_ready``: when a user has ≥ N unabsorbed
            facts, call the LLM to draft reflections, persist them
            with status='pending', and mark the source facts absorbed.

  Stage 3 — ``promote_pending``: scan reflections older than
            ``promote_age_days`` whose evidence has no disp signal,
            upsert them into ``user_profiles`` with
            ``source='reflection_promote'``, and flip status='promoted'.

A 4th "manual" path covers the WebUI buttons: ``promote_reflection``
forces immediate promotion; ``deny_reflection`` records a disputation
signal (preventing future auto-promote and pushing the reflection
toward archive_candidate).

The reconciler holds no state of its own — it's a façade over the
plugin's db + llm client + EvidenceConfig. Safe to construct many
times; the only state is the bounded semaphore on the synthesis LLM
calls and that's lazy.
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Optional

from core.logging_manager import get_logger

from .evidence import (
    EvidenceConfig,
    EvidenceSnapshot,
    evidence_score,
    derive_status,
)
from .reflection import (
    ReflectionDraft,
    synthesize_reflections,
)

logger = get_logger("kiraos_reconciler", "green")


@dataclass
class ReconcilerResult:
    """Telemetry returned by each reconciler stage. Fields are optional
    so the same shape can describe synth runs (writes counts) and
    promote runs (promotions / denials). Callers (Phase 5 WebUI, logs)
    read what they care about; absent fields just stay zero.
    """
    stage: str = ""
    user_id: Optional[str] = None
    facts_considered: int = 0
    reflections_drafted: int = 0
    reflections_persisted: int = 0
    facts_absorbed: int = 0
    promotions: int = 0
    denials: int = 0
    skipped_reason: Optional[str] = None
    errors: list[str] = field(default_factory=list)


class Reconciler:
    """Façade over (db, llm_client_provider, EvidenceConfig).

    Each public method is async, never raises (errors land in
    ``ReconcilerResult.errors``), and is independently feature-flagged
    by the plugin layer — the reconciler trusts its caller to gate
    calls on the relevant ``enable_*`` switches.
    """

    def __init__(
        self,
        db,  # UserMemoryDB; loose typing avoids import cycle
        llm_client_provider,  # 0-arg callable → client | None
        evidence_config: EvidenceConfig,
        *,
        min_facts: int = 5,
        promote_age_days: float = 3.0,
        synthesis_timeout_s: float = 20.0,
        max_inflight: int = 2,
        auditor_confidence_cap: float = 0.7,
    ):
        self.db = db
        self._llm_client_provider = llm_client_provider
        self.evidence_config = evidence_config
        self.min_facts = max(2, int(min_facts))
        self.promote_age_days = float(promote_age_days)
        self.synthesis_timeout_s = float(synthesis_timeout_s)
        self.max_inflight = max(1, int(max_inflight))
        self.auditor_confidence_cap = float(auditor_confidence_cap)
        self._synth_semaphore: Optional[asyncio.Semaphore] = None

    # ── Stage 2: synthesize ────────────────────────────────────────

    async def synthesize_if_ready(self, user_id: str) -> ReconcilerResult:
        """Conditionally run the LLM synthesis for one user.

        Cheap when the user doesn't have enough unabsorbed facts —
        we count first, then bail without an LLM call. Cap concurrent
        synthesis calls via the semaphore so a multi-user burst can't
        overwhelm the provider.
        """
        out = ReconcilerResult(stage="synthesize", user_id=user_id)
        try:
            unabsorbed = self.db.list_unabsorbed_events(
                user_id, limit=max(20, self.min_facts * 4),
            )
        except Exception as exc:
            out.errors.append(f"list_unabsorbed_events: {exc}")
            return out
        out.facts_considered = len(unabsorbed)
        if len(unabsorbed) < self.min_facts:
            out.skipped_reason = (
                f"only {len(unabsorbed)} unabsorbed facts "
                f"(need ≥ {self.min_facts})"
            )
            return out

        client = None
        try:
            client = self._llm_client_provider() if self._llm_client_provider else None
        except Exception as exc:
            out.errors.append(f"client_provider: {exc}")
            return out
        if client is None:
            out.skipped_reason = "no LLM client available"
            return out

        if self._synth_semaphore is None:
            # Lazy build on the running loop. Same pattern as the auditor.
            self._synth_semaphore = asyncio.Semaphore(self.max_inflight)

        async with self._synth_semaphore:
            drafts = await synthesize_reflections(
                client, unabsorbed, timeout_s=self.synthesis_timeout_s,
            )
        out.reflections_drafted = len(drafts)
        if not drafts:
            return out

        # Persist each draft + mark its source facts absorbed. We do
        # this in a tight loop rather than one big transaction so a
        # malformed draft only loses one reflection, not the batch.
        persisted = 0
        absorbed_total = 0
        for draft in drafts:
            try:
                rid = self.db.save_reflection(
                    user_id, draft.summary,
                    entity=draft.entity,
                    relation_type=draft.relation_type,
                    source_fact_ids=draft.source_fact_ids,
                    status="pending",
                )
                # Seed initial rein from the max source-fact importance.
                # Without this, every reflection starts at score=0 and
                # needs explicit signals to reach `confirmed_threshold` —
                # but a draft built on importance=10 facts (nicknames,
                # "请记住 X") should fast-track.
                max_imp = self._max_importance_for(draft.source_fact_ids)
                if max_imp >= 7:
                    from .evidence import initial_reinforcement_from_importance
                    seed = initial_reinforcement_from_importance(
                        max_imp, self.evidence_config
                    )
                    if seed > 0:
                        self.db.record_evidence_signal(
                            "reflection", str(rid),
                            rein_delta=seed,
                            source="reflection_seed",
                        )
                affected = self.db.mark_facts_absorbed(draft.source_fact_ids)
                persisted += 1
                absorbed_total += affected
            except Exception as exc:
                out.errors.append(f"persist draft: {exc}")
        out.reflections_persisted = persisted
        out.facts_absorbed = absorbed_total
        return out

    def _max_importance_for(self, fact_ids: list[int]) -> int:
        """Look up the importance ceiling for a fact-id set. Returns
        the max ``event_logs.importance`` value, falling back to 5
        (the schema default) if none of the ids resolve.
        """
        if not fact_ids:
            return 5
        placeholders = ",".join("?" * len(fact_ids))
        conn = self.db._get_conn()
        cursor = conn.execute(
            f"SELECT MAX(importance) FROM event_logs WHERE id IN ({placeholders})",
            fact_ids,
        )
        row = cursor.fetchone()
        if row is None or row[0] is None:
            return 5
        return int(row[0])

    # ── Stage 3: promote ───────────────────────────────────────────

    async def promote_pending(
        self,
        *,
        max_promotions: int = 50,
    ) -> ReconcilerResult:
        """Sweep pending reflections older than ``promote_age_days``,
        promote the ones with no disputation signal.

        We bound the per-tick promotion count so a backlog (e.g.
        synthesis ran on a fresh DB with thousands of facts) doesn't
        cause a single tick to consume an unbounded amount of work.
        Subsequent ticks pick up where this one left off.

        Returns a ReconcilerResult — caller logs it for telemetry.
        Errors per-row are caught and stored, not propagated.
        """
        out = ReconcilerResult(stage="promote")
        cutoff_age = self.promote_age_days * 86400.0
        now_epoch = int(time.time())
        try:
            candidates = self.db.list_promotion_candidates(
                age_seconds=cutoff_age,
                limit=max_promotions * 4,  # over-fetch — many will be filtered
            )
        except Exception as exc:
            out.errors.append(f"list_promotion_candidates: {exc}")
            return out

        promotions = 0
        denials_observed = 0
        for ref in candidates:
            if promotions >= max_promotions:
                break
            try:
                snap_dict = self.db.get_evidence_snapshot("reflection", str(ref["id"]))
            except Exception as exc:
                out.errors.append(f"snapshot {ref['id']}: {exc}")
                continue
            # Build a snapshot for the pure-fn score check.
            snap = EvidenceSnapshot(
                rein=snap_dict["rein"] if snap_dict else 0.0,
                disp=snap_dict["disp"] if snap_dict else 0.0,
                rein_last_signal_at=snap_dict["rein_last_signal_at"] if snap_dict else None,
                disp_last_signal_at=snap_dict["disp_last_signal_at"] if snap_dict else None,
            )
            score = evidence_score(snap, now_epoch, self.evidence_config)
            status = derive_status(snap, now_epoch, self.evidence_config)

            # Hard veto: any active disputation blocks auto-promotion.
            # The user can still hit "promote" manually via the WebUI
            # if they explicitly want to override.
            if snap.disp > 0:
                denials_observed += 1
                continue
            # Auto-promotion requires the score to clear `confirmed`
            # (not `promoted` — promoted is reserved for a stronger
            # threshold that the reconciler MAY enforce in the future).
            if status not in ("confirmed", "promoted"):
                continue

            ok = self._promote_reflection_row(ref)
            if ok:
                promotions += 1
        out.promotions = promotions
        out.denials = denials_observed
        return out

    def _promote_reflection_row(self, ref: dict) -> bool:
        """Persist one reflection promotion. Returns True iff a profile
        row was created or updated.

        Profile key: ``reflection_<id>`` keeps the namespace clean and
        survives later edits — the WebUI / consolidate_memory tool can
        identify reflection-sourced rows at a glance. Caller is free
        to rename via `memory_update(op='set')` later.
        """
        try:
            rid = ref["id"]
            summary = ref["summary"]
            # Confidence: we cap at the auditor's ceiling so a stream
            # of reflection promotions can't out-confidence human-
            # confirmed memory_update(force=true) entries.
            confidence = self.auditor_confidence_cap
            category = self._category_for_relation(ref.get("relation_type"))
            key = f"reflection_{rid}"
            status, info = self.db.upsert_with_limit(
                ref.get("user_id") or "",
                key, summary,
                max_profiles=10_000,  # large — promotion shouldn't be capped
                confidence=confidence,
                category=category,
            )
            if status in ("set", "updated", "truncated"):
                # Stamp metadata so the WebUI can distinguish source.
                # We write source separately because upsert_with_limit
                # doesn't expose the new Phase 3a columns.
                self._stamp_profile_source(
                    ref.get("user_id") or "", key,
                    source="reflection_promote",
                    entity=ref.get("entity"),
                    relation_type=ref.get("relation_type"),
                )
                self.db.update_reflection_status(
                    rid, "promoted", promoted_at=int(time.time()),
                )
                return True
            return False
        except Exception as exc:
            logger.warning(f"promote reflection failed: {exc}")
            return False

    def _stamp_profile_source(
        self, user_id: str, key: str, *,
        source: str, entity: Optional[str], relation_type: Optional[str],
    ) -> None:
        """Set the Phase 3a metadata columns directly. Used immediately
        after an upsert so the WebUI / persona-graph code can rely
        on these being populated for reflection-promoted rows.
        """
        with self.db._write_lock:
            conn = self.db._get_conn()
            conn.execute(
                "UPDATE user_profiles SET source = ?, entity = ?, "
                "relation_type = ? "
                "WHERE user_id = ? AND memory_key = ?",
                (source, entity, relation_type, user_id, key),
            )
            conn.commit()

    @staticmethod
    def _category_for_relation(relation_type: Optional[str]) -> str:
        """Map a free-text relation_type onto one of the existing
        category buckets so injection rules / consolidate_memory keep
        working uniformly.

        Unknown relation types fall through to ``other``. The mapping
        is deliberately small — the LLM tends to invent novel
        relation types, and forcing them through a small enum keeps
        the persona view comprehensible.
        """
        if not relation_type:
            return "other"
        r = relation_type.lower()
        if any(k in r for k in ("identity", "name", "id", "身份", "昵称")):
            return "basic"
        if any(k in r for k in ("hobby", "preference", "like", "love",
                                "兴趣", "爱好", "喜好")):
            return "preference"
        if any(k in r for k in ("relation", "family", "friend", "关系",
                                "亲属", "朋友", "social")):
            return "social"
        return "other"

    # ── Manual operations (WebUI hooks) ────────────────────────────

    def promote_reflection(self, reflection_id: int, user_id: str) -> bool:
        """Force-promote one reflection regardless of evidence/age.
        Used by the WebUI's manual promote button (Phase 5) and the
        REST endpoint added below.
        """
        rows = self.db.list_reflections(user_id, limit=100_000)
        match = next((r for r in rows if r["id"] == reflection_id), None)
        if match is None:
            return False
        match["user_id"] = user_id
        return self._promote_reflection_row(match)

    def deny_reflection(self, reflection_id: int, user_id: str) -> bool:
        """Record a disp signal on a reflection and flip its status
        to 'denied'. Future auto-promotion sweeps will skip it.
        """
        rows = self.db.list_reflections(user_id, limit=100_000)
        match = next((r for r in rows if r["id"] == reflection_id), None)
        if match is None:
            return False
        try:
            self.db.record_evidence_signal(
                "reflection", str(reflection_id),
                disp_delta=1.0,
                source="user_directive",
            )
        except Exception as exc:
            logger.warning(f"deny signal failed for {reflection_id}: {exc}")
        return self.db.update_reflection_status(reflection_id, "denied")
