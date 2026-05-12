"""Evidence math (Evidence RFC, derived from NEKO/memory/evidence.py).

The whole module is pure functions of (current state, now, config).
Nothing here touches the DB; the caller is responsible for loading a
ledger row, passing it in, and writing back the new snapshot if needed.

Two cardinal rules from the RFC, reproduced verbatim from the source
implementation we're porting:

  1. **Decay is read-time.** ``evidence_score()`` recomputes from the
     stored ``rein`` + ``disp`` + last-signal timestamps every read.
     We never persist a "current decayed value" — the truth is the raw
     counter and the half-life parameter at the moment the question
     is asked.

  2. **Independent clocks.** Reinforcement and disputation have their
     own ``last_signal_at`` fields. A new ``disp`` signal does not
     re-anchor the ``rein`` decay timer (and vice versa). This is what
     makes "I love cats" → 3 weeks later → "actually I'm allergic"
     behave correctly: the early reinforcement keeps decaying on its
     own schedule while the new disputation accumulates from now.

The ``protected`` shortcut at the top of ``evidence_score`` lets future
imports from character cards (Phase 3b) flag a row as "always
authoritative" — those rows return ``+inf`` and never get archived.

Constants are passed in from the plugin's config (schema.json) so users
can tune the half-life without touching code. Defaults match NEKO.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional


# Tier labels returned by derive_status. Stored only transiently.
STATUS_PROMOTED = "promoted"
STATUS_CONFIRMED = "confirmed"
STATUS_PENDING = "pending"
STATUS_ARCHIVE_CANDIDATE = "archive_candidate"


@dataclass
class EvidenceConfig:
    """Half-life + threshold parameters resolved from cfg at plugin
    init time. Passed into every read-time computation so each call
    self-describes its assumptions — easier to reason about in the
    debugging WebUI (Phase 5) where users can preview "what if I
    halved the disp half-life?".
    """
    rein_half_life_days: float = 14.0
    disp_half_life_days: float = 7.0
    promoted_threshold: float = 1.0
    confirmed_threshold: float = 0.3
    archive_threshold: float = -0.5
    # Combo bonus (RFC §3.1.8) — once a fact has been reinforced via
    # user_fact signals more than ``user_fact_combo_threshold`` times,
    # each additional reinforce adds the bonus on top of the base rein
    # delta. Caps fast-tracking on persistent user emphasis.
    user_fact_combo_threshold: int = 2
    user_fact_combo_bonus: float = 0.5
    # Importance-to-initial-rein seed table (RFC §3.1.2 exception). One
    # high-importance fact in a reflection's source set should
    # fast-track it through pending → confirmed → promoted, so the
    # reflection starts with a non-zero rein baseline. Keys are
    # importance integers ≥ threshold; values are the seed.
    importance_seed: tuple[tuple[int, float], ...] = (
        (10, 0.8), (9, 0.6), (8, 0.4), (7, 0.2),
    )


@dataclass
class EvidenceSnapshot:
    """A persisted ledger row, decoupled from the SQLite tuple shape.

    All fields are post-write; effective values are computed from these
    at read time. ``last_signal_at`` is unix epoch seconds; ``None``
    means "no signal of that polarity yet, age is infinite (or zero —
    doesn't matter, the corresponding base counter is also zero)."

    sub_zero_days and user_fact_reinforce_count are part of NEKO's
    funnel analytics and combo logic respectively; we carry them in
    the snapshot so the math stays compatible with future imports.
    """
    rein: float = 0.0
    disp: float = 0.0
    rein_last_signal_at: Optional[int] = None
    disp_last_signal_at: Optional[int] = None
    sub_zero_days: float = 0.0
    user_fact_reinforce_count: int = 0
    protected: bool = False


def _age_days(last_signal_at: Optional[int], now_epoch: int) -> float:
    """Age in days of a last-signal-at timestamp at ``now_epoch``.

    Clock-skew clamp: negative ages (last_signal_at in the future, e.g.
    after a system clock rollback or a migration from a host with a
    different timezone interpretation) clamp to zero rather than
    producing exponential *growth* of the effective value.
    """
    if last_signal_at is None:
        return 0.0
    delta = now_epoch - int(last_signal_at)
    if delta <= 0:
        return 0.0
    return delta / 86400.0


def effective_reinforcement(snap: EvidenceSnapshot, now_epoch: int,
                            config: EvidenceConfig) -> float:
    """Apply half-life decay to the stored rein counter.

    Half-life formula: ``rein * 0.5 ** (age_days / half_life_days)``.
    A row that hasn't been reinforced in one half-life keeps half its
    original strength; in two half-lives, a quarter; and so on.
    """
    if snap.rein == 0.0:
        return 0.0
    age = _age_days(snap.rein_last_signal_at, now_epoch)
    if age == 0.0:
        return float(snap.rein)
    half = config.rein_half_life_days or 1.0
    return float(snap.rein) * (0.5 ** (age / half))


def effective_disputation(snap: EvidenceSnapshot, now_epoch: int,
                          config: EvidenceConfig) -> float:
    """Symmetric to ``effective_reinforcement``, using disp half-life."""
    if snap.disp == 0.0:
        return 0.0
    age = _age_days(snap.disp_last_signal_at, now_epoch)
    if age == 0.0:
        return float(snap.disp)
    half = config.disp_half_life_days or 1.0
    return float(snap.disp) * (0.5 ** (age / half))


def evidence_score(snap: EvidenceSnapshot, now_epoch: int,
                   config: EvidenceConfig) -> float:
    """Net evidence (positive minus negative) at ``now_epoch``.

    ``protected=True`` rows short-circuit to +inf — they're frozen-in
    facts that no decay or disputation should ever knock loose. This
    lets character-card imports (Phase 3b) pin "Alice's name is Alice"
    against accidental erosion.
    """
    if snap.protected:
        return math.inf
    return (effective_reinforcement(snap, now_epoch, config)
            - effective_disputation(snap, now_epoch, config))


def derive_status(snap: EvidenceSnapshot, now_epoch: int,
                  config: EvidenceConfig) -> str:
    """Map a numeric evidence_score onto the tier labels Phase 3b uses.

    Tier boundaries are caller-tunable via EvidenceConfig — when the
    user moves the half-life slider in the Phase 5 WebUI, this is what
    flips a row from 'pending' to 'confirmed' without any state write.
    """
    s = evidence_score(snap, now_epoch, config)
    if s >= config.promoted_threshold:
        return STATUS_PROMOTED
    if s >= config.confirmed_threshold:
        return STATUS_CONFIRMED
    if s <= config.archive_threshold:
        return STATUS_ARCHIVE_CANDIDATE
    return STATUS_PENDING


def initial_reinforcement_from_importance(
    max_importance: int, config: EvidenceConfig,
) -> float:
    """Seed a reflection's initial rein from the max-importance of its
    source facts. See EvidenceConfig.importance_seed for the table.

    "Max" not "average": one nickname-level fact (importance=10) in a
    reflection's source set should fast-track the reflection through
    the funnel even if it's mixed in with five mundane importance=3
    observations. Averaging would dilute the signal we want to keep.
    """
    try:
        imp = int(max_importance)
    except (TypeError, ValueError):
        return 0.0
    for threshold, seed in config.importance_seed:
        if imp >= threshold:
            return float(seed)
    return 0.0


@dataclass
class EvidenceDelta:
    """Apply-pending delta produced by a signal source (user_fact,
    user_dispute, manual_set, …). Combines with an existing snapshot
    in ``compute_evidence_snapshot``.
    """
    rein_delta: float = 0.0
    disp_delta: float = 0.0
    source: str = "auto"


def compute_evidence_snapshot(
    snap: EvidenceSnapshot, delta: EvidenceDelta,
    now_epoch: int, config: EvidenceConfig,
) -> EvidenceSnapshot:
    """Pure: ``(state, delta) → new state``.

    Implements the RFC's combo-bonus logic for ``source == 'user_fact'``
    (most common signal class):

      - Each new positive rein adds ``rein_delta`` plus, once the
        counter exceeds ``user_fact_combo_threshold``, an extra
        ``user_fact_combo_bonus``. The counter never decrements, so
        long-running combos durably accelerate future reinforces of
        the same target.

    Disputation is non-negative — a negative disp_delta (which would
    represent "the user took back their objection") clamps to zero
    rather than going below. Reinforcement can go negative through
    explicit ``rein_delta < 0`` writes; those are rare but valid.

    Returns a NEW snapshot — caller decides whether/how to persist.
    """
    new_rein = float(snap.rein) + float(delta.rein_delta)
    new_disp = float(snap.disp) + float(delta.disp_delta)
    if new_disp < 0.0:
        new_disp = 0.0

    # Independent clocks: only stamp the side that moved this turn.
    new_rein_ts = snap.rein_last_signal_at
    new_disp_ts = snap.disp_last_signal_at
    if delta.rein_delta != 0.0:
        new_rein_ts = now_epoch
    if delta.disp_delta != 0.0:
        new_disp_ts = now_epoch

    new_count = snap.user_fact_reinforce_count
    if delta.source == "user_fact" and delta.rein_delta > 0:
        new_count += 1
        if new_count > config.user_fact_combo_threshold:
            new_rein += float(config.user_fact_combo_bonus)

    return EvidenceSnapshot(
        rein=new_rein,
        disp=new_disp,
        rein_last_signal_at=new_rein_ts,
        disp_last_signal_at=new_disp_ts,
        sub_zero_days=snap.sub_zero_days,
        user_fact_reinforce_count=new_count,
        protected=snap.protected,
    )


def make_rein_delta(weight: float = 0.5, source: str = "auto") -> EvidenceDelta:
    """Convenience constructor for a positive-rein signal. The default
    weight of 0.5 matches NEKO's base rein delta — a single confirmation
    is mildly positive; sustained repetition is what builds a reflection
    up to ``promoted_threshold``."""
    return EvidenceDelta(rein_delta=float(weight), source=source)


def make_disp_delta(weight: float = 0.5, source: str = "user_directive") -> EvidenceDelta:
    """Convenience constructor for a disputation signal. The default
    weight matches a single user objection; large weights are reserved
    for explicit ``memory_update(force=true)`` overwrites and direct
    deny-via-WebUI actions in Phase 3b."""
    return EvidenceDelta(disp_delta=float(weight), source=source)
