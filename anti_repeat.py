"""Anti-repeat corpus — detects topic words the AI has been over-using.

The classical "AI keeps saying X" problem: across a session, certain
phrases creep into nearly every assistant response (sign-offs, hedges,
favourite metaphors). The user notices well before the model does.

NEKO's solution (we borrow the shape, not the data layout): keep a
bounded ring of recent assistant outputs, compute document-frequency
of "topic words" across them, and inject a one-line system hint
listing the top offenders before the next LLM call. The model then
explicitly avoids them, breaking the repetition cycle.

In-memory by design:
  - Anti-repeat is a *generation* signal, not a *memory* fact. Losing
    history on restart is fine — the corpus rebuilds within K turns.
  - Skipping persistence means zero migration concerns and zero
    pollution of event_logs / persona.

Per-user vs global: we keep per-user buffers. Two users in the same
plugin session would otherwise share an over-used-words list, which
would inject one user's pet phrases into another's prompt context.
The cost is tiny — at most ``max_size`` strings per user.

Tokenization is simple-by-design. CJK runs are sliced into bigrams
(``"今天天气" → ["今天", "天天", "天气"]``) and ASCII words come from
``re.findall(r"\\w+", ...)``. Stop tokens cover the most common high-DF
fillers that aren't repetition-worthy. Calling out "the" as an over-used
token would be useless; calling out "it's worth noting" actually helps.
"""
from __future__ import annotations

import re
import time
from collections import deque
from typing import Optional


# Stopwords: tokens we never flag as "over-used" because they're
# linguistically high-frequency rather than stylistically repetitive.
# Kept small on purpose — fashion-vocab like "fundamentally" or
# "essentially" SHOULD show up as over-used when the model leans on it.
_STOP_TOKENS = frozenset({
    # English fillers
    "the", "a", "an", "and", "or", "but", "is", "are", "was", "were",
    "of", "to", "in", "on", "at", "for", "with", "by", "from", "as",
    "it", "this", "that", "these", "those", "be", "have", "has", "had",
    "do", "does", "did", "can", "could", "would", "should", "will",
    "you", "your", "i", "we", "they", "them", "us", "he", "she",
    "not", "no", "yes",
    # CJK fillers (bigrams)
    "我们", "你们", "他们", "它们", "因为", "所以", "如果", "但是",
    "不过", "可以", "可能", "应该", "需要", "知道", "觉得", "感觉",
    "这个", "那个", "什么", "怎么", "怎样", "为什么",
})

# Pre-compiled splitter: a "block" is either ASCII word run, CJK Han
# run, or a single non-token character we discard. We split on the
# block boundaries and then tokenize each block per its kind.
_BLOCK_RE = re.compile(r"[a-zA-Z0-9_]+|[一-鿿]+", re.UNICODE)


def _tokenize_text(text: str, *, min_len: int = 4) -> set[str]:
    """Return the DISTINCT tokens in ``text`` (set, not list).

    Distinct-per-document is what DF means — repeating "however"
    five times in one paragraph shouldn't inflate the score; what we
    care about is whether multiple separate responses lean on it.

    ``min_len`` filters out very short tokens that tend to be noise.
    The default 4 lets CJK bigrams (2 chars × 2 bytes UTF-16 view —
    but we count code points, so 2-char CJK has ``len == 2``, which
    is excluded). Lower this if you need bigram coverage; default
    keeps CJK trigrams (3-char) and ASCII 4+ letter words.
    """
    if not text:
        return set()
    out: set[str] = set()
    for block in _BLOCK_RE.findall(text):
        # Detect ASCII vs CJK by the first character — within a block
        # the regex guarantees homogeneity.
        first = block[0]
        if first.isascii():
            tok = block.lower()
            if len(tok) >= min_len and tok not in _STOP_TOKENS:
                out.add(tok)
        else:
            # CJK run — emit trigrams. Trigrams catch idiomatic
            # phrases ("毫无疑问", "话说回来") and align with the FTS5
            # trigram tokenizer we use elsewhere in this plugin.
            for i in range(len(block) - 2):
                tok = block[i: i + 3]
                if tok in _STOP_TOKENS:
                    continue
                out.add(tok)
    return out


class AntiRepeatCorpus:
    """Per-user bounded ring of recent assistant outputs.

    Append-only at write time. Reads (``overused_tokens``) lazily
    re-tokenize the last ``lookback`` entries — no precomputed index
    because the buffers are small (default ≤ 100 entries) and the
    tokenizer is fast. Avoiding an index keeps the data structure
    immutable-modulo-the-deque, so concurrent access from the
    auditor task + the llm_request hook doesn't need locking
    beyond what asyncio already gives us.
    """

    def __init__(self, max_size: int = 100):
        # One deque per user. The dict itself is small (one entry per
        # active user); deques cap at ``max_size`` so total memory
        # stays bounded even for long sessions.
        self._buffers: dict[str, deque] = {}
        self._max_size = max(1, int(max_size))

    def record(self, user_id: str, text: str) -> None:
        """Append an assistant output for this user. No-op for empty
        text — short responses (especially tool-call-only turns with
        empty raw_output) shouldn't dilute the DF signal.
        """
        if not text or not text.strip():
            return
        buf = self._buffers.get(user_id)
        if buf is None:
            buf = deque(maxlen=self._max_size)
            self._buffers[user_id] = buf
        buf.append((int(time.time()), text))

    def overused_tokens(
        self,
        user_id: str,
        *,
        lookback: int = 10,
        df_threshold: int = 5,
        max_emit: int = 8,
    ) -> list[str]:
        """Return up to ``max_emit`` tokens whose document-frequency
        across the last ``lookback`` entries meets ``df_threshold``.

        Token order is by DF descending, ties broken alphabetically
        (stable across calls so the hint text doesn't churn when the
        underlying signal hasn't actually changed).

        ``df_threshold`` defaults to half of ``lookback`` so a token
        used in ≥ half of recent responses is flagged. Callers tune
        via the plugin's schema.json knobs.
        """
        buf = self._buffers.get(user_id)
        if not buf:
            return []
        recent = list(buf)[-max(1, int(lookback)):]
        if len(recent) < df_threshold:
            # Not enough samples yet to reach the threshold — no point
            # in returning a possibly-noisy partial signal.
            return []
        df: dict[str, int] = {}
        for _, text in recent:
            for tok in _tokenize_text(text):
                df[tok] = df.get(tok, 0) + 1
        offenders = [
            (tok, count) for tok, count in df.items()
            if count >= df_threshold
        ]
        if not offenders:
            return []
        offenders.sort(key=lambda t: (-t[1], t[0]))
        return [tok for tok, _ in offenders[: max(1, int(max_emit))]]

    def clear(self, user_id: Optional[str] = None) -> None:
        """Drop the corpus for one user, or all users when omitted.
        Exposed so memory_clear can wipe the buffer alongside the
        DB state — otherwise an "I forgot you" tool call would still
        carry the user's verbal fingerprint into the next session.
        """
        if user_id is None:
            self._buffers.clear()
        else:
            self._buffers.pop(user_id, None)

    def size(self, user_id: Optional[str] = None) -> int:
        """Test/debug helper: current corpus size for one user, or
        total entries across all users when called without an id.
        """
        if user_id is None:
            return sum(len(buf) for buf in self._buffers.values())
        buf = self._buffers.get(user_id)
        return len(buf) if buf else 0


def format_anti_repeat_hint(tokens: list[str]) -> str:
    """Render the list of over-used tokens as a system-prompt-shaped
    one-liner. Returns empty string when there's nothing to flag so
    the caller can skip injection entirely with a falsy check.
    """
    if not tokens:
        return ""
    quoted = "、".join(f"「{t}」" for t in tokens)
    return (
        f"🔁 最近几条回复中你反复使用了 {quoted}，"
        "本轮请换种表达方式，避免复读。"
    )
