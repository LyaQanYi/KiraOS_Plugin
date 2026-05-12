"""Fact identity + dedup for the Tier-1 layer (event_logs).

A single ``fact_hash`` derived from a normalized form of the event
summary is the unit of identity. Two events that say "the user loves
cats" — modulo whitespace, punctuation, case, and a handful of
filler words — should collapse onto the same hash and thus the same
ledger entry.

Normalization is intentionally conservative: we don't pull in an NLP
stemmer or a CJK segmenter (would add heavy dependencies and require
NEKO-style stop-name lists per character). What we *do* normalize:

  - Unicode NFKC: fold full-width punctuation to its ASCII form so
    "今天去跑步了！" and "今天去跑步了!" hash the same.
  - Lowercase: case shouldn't make "Loves Cats" a different fact from
    "loves cats".
  - Collapse internal whitespace runs to single spaces.
  - Strip a small set of leading/trailing punctuation.
  - Strip common conversational suffixes ("吧", "了", "啊", "呢"…)
    that don't carry semantic load — keeps "用户喜欢猫了" the same
    as "用户喜欢猫". This is the only language-aware step, kept tiny
    and explicit.

That's it. False merges are far less harmful than false splits for
this use case: a merged fact still surfaces in recall, but a split
fact spawns redundant reflections that confuse the persona.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from typing import Optional


# Conservative trailing-filler list. Each must be a single character —
# multi-char sequences would risk merging substantively different
# events. CJK exclamation/period are already handled by the punctuation
# strip below.
_TRAILING_FILLERS = "了啊呢吧呀哦哈"

# Leading/trailing punctuation to strip after NFKC + lowercasing. We
# keep this whitelist short on purpose; an aggressive strip would erode
# semantic content like negation marks.
_BOUNDARY_PUNCT = ' \t\n\r.,;:!?，。；：！？、…—-—_~`"\'"”“‘’()[]{}（）【】'

# Regex used to collapse internal whitespace runs.
_WS_RUN = re.compile(r"\s+", re.UNICODE)


def normalize_fact_text(text: str) -> str:
    """Return the canonical form used by :func:`fact_hash`.

    Public so callers (tests, the WebUI debugging tools in Phase 5)
    can preview "what would two events normalize to" without having
    to compute and compare hashes.
    """
    s = "" if text is None else str(text)
    if not s:
        return ""
    # 1) Unicode normalization (full-width → half-width, compatibility
    #    composition). NFKC is the strongest of the four — appropriate
    #    here because we want maximum tolerance for input variations.
    s = unicodedata.normalize("NFKC", s)
    # 2) Lowercase. NFKC + lower handles "Café" / "café" / "CAFÉ".
    s = s.lower()
    # 3) Collapse whitespace.
    s = _WS_RUN.sub(" ", s).strip()
    # 4) Strip boundary punctuation. We loop because consecutive
    #    runs would otherwise leave one behind: "(hello)." → "hello)"
    #    after one pass.
    while s and s[0] in _BOUNDARY_PUNCT:
        s = s[1:]
    while s and s[-1] in _BOUNDARY_PUNCT:
        s = s[:-1]
    # 5) Strip a single trailing CJK filler character if present.
    #    Iterated so "好的呢吧" → "好的".
    while s and s[-1] in _TRAILING_FILLERS:
        s = s[:-1]
    return s


def fact_hash(text: str, *, salt: str = "") -> str:
    """SHA-256 hex digest of the normalized fact text.

    A non-empty ``salt`` is mixed in before hashing — useful for
    user-scoped namespaces if a future Phase decides to partition fact
    identity by user (e.g. so two users repeating the same idiom don't
    collide in shared persona analytics). Defaults to no salt; Phase 3a
    keeps fact_hash global because the (user_id, fact_hash) tuple in
    the DB index is already a natural composite key.

    Returns ``""`` for empty/whitespace-only input so callers can use
    the falsy result to short-circuit the dedup logic instead of
    hashing the empty string into a fixed sentinel.
    """
    norm = normalize_fact_text(text)
    if not norm:
        return ""
    h = hashlib.sha256()
    if salt:
        h.update(salt.encode("utf-8"))
        h.update(b"\x00")
    h.update(norm.encode("utf-8"))
    return h.hexdigest()


def importance_from_text(text: str, ceiling: int = 10) -> int:
    """Heuristic importance scoring for an event summary.

    Real NEKO computes importance via LLM during fact extraction; we
    don't have that signal at memory_update(op='event') time, so we
    fall back to a tiny rule-based scorer that the auditor / reflection
    synthesis in Phase 3b will refine. The defaults are tuned so the
    Phase 3a evidence math sees plausible numbers without the LLM:

      - Long detailed events → higher (more substance)
      - Events tagged with one of the "important" tags → higher
      - Events with negation / "don't" / "不要" → unchanged (these
        are valid facts, not noise)
      - Otherwise: 5 (neutral baseline)

    Bounded to [1, ``ceiling``] so a single weird input can't push
    downstream evidence math into pathological territory.
    """
    if not text:
        return 1
    score = 5
    n_chars = len(text)
    if n_chars >= 60:
        score += 2
    elif n_chars >= 30:
        score += 1
    # Markers of explicit user requests to remember (these tend to
    # arrive with rein_delta amplified by the user_directive source).
    # Kept tiny and language-specific; the LLM-driven path replaces
    # this in Phase 3b.
    if any(kw in text for kw in ("请记住", "记得", "重要", "remember", "important")):
        score += 2
    if score < 1:
        score = 1
    if score > ceiling:
        score = ceiling
    return score


def is_dedup_candidate(text: str) -> bool:
    """True when the input is substantive enough to make hashing
    worthwhile. Very short snippets ("好", "嗯", "ok") tend to occur in
    unrelated contexts; collapsing them under one hash would over-merge
    and starve the funnel.
    """
    norm = normalize_fact_text(text)
    return len(norm) >= 3


__all__ = [
    "normalize_fact_text",
    "fact_hash",
    "importance_from_text",
    "is_dedup_candidate",
]
