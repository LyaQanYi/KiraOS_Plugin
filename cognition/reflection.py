"""Tier-2 reflection synthesis: from atomic facts to higher-order insights.

A reflection is a one-sentence claim about a user that's grounded in
≥ 2 atomic facts. Example: given three event_logs rows mentioning
running (跑步, 公里, marathon), the synthesizer emits "用户最近开始固定
跑步" with ``source_fact_ids = [eid1, eid2, eid3]``. The reconciler
(reconciler.py) then watches that pending reflection's evidence score
and either auto-promotes it to persona (Tier 3) or lets it decay.

Module shape mirrors ``embeddings.py`` / ``recall.py`` — a thin
dataclass + parser + a stateless ``synthesize`` coroutine that takes
an LLM client. No DB I/O lives here; the reconciler is the only
boundary that talks to both.
"""
from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from typing import Optional, Sequence

from core.logging_manager import get_logger

logger = get_logger("kiraos_reflection", "green")


# Prompt — terse on purpose. The LLM gets a numbered fact list and is
# expected to return JSON only. We over-specify the schema to maximize
# parseability; over-specifying the "what to look for" semantics would
# bias the model toward forced syntheses.
SYNTHESIS_PROMPT = """\
你是一个用户记忆反思助手。下面是用户最近的若干条原子事件（fact）。
任务：识别其中"出现≥2次的稳定模式或可总结的高阶洞察"，并把这种模式总结成一句话。

【硬性规则】
1. 只对**至少 2 条事件共同支持**的模式做反思，单条事件不要反思
2. 跳过：单次/偶发事件、客套话、玩笑、用户的负面情绪发泄
3. 一句反思最多 60 字；不要复述事件原文，要抽象出"用户长期/反复…"
4. 输出**严格的 JSON 数组**，无 markdown 围栏、无解释
5. 没有可总结的就输出 `[]`

【输出格式】
[
  {"summary": "<≤60字>",
   "source_fact_ids": [<int>, <int>, ...],
   "entity": "<可选: 实体名, 比如 'user'>",
   "relation_type": "<可选: 比如 'hobby'/'preference'/'identity'>"}
]

【字段说明】
- source_fact_ids: 支撑这条反思的事件 id 列表（来自下面输入），≥2
- entity / relation_type: 可空字符串。把同一类反思归到一起便于后续 promote

【输入事件】
{facts_block}

只输出 JSON 数组。"""


@dataclass
class ReflectionDraft:
    """An LLM-proposed reflection before it's persisted.

    Carries provenance (the source_fact_ids that the model claims
    support its claim) so the reconciler can mark those facts
    absorbed atomically with the reflection insert.
    """
    summary: str
    source_fact_ids: list[int] = field(default_factory=list)
    entity: Optional[str] = None
    relation_type: Optional[str] = None


def _strip_json_fence(text: str) -> str:
    """Best-effort strip of ```/```json fences. Mirrors the auditor's
    parser; kept local to this module so reflection.py has no
    cross-package dependency on the auditor's helpers."""
    if not text:
        return ""
    s = text.strip()
    m = re.match(r"^```(?:json)?\s*\n?", s, flags=re.IGNORECASE)
    if m:
        s = s[m.end():]
    if s.endswith("```"):
        s = s[:-3].rstrip()
    return s


def parse_reflection_output(text: str, valid_fact_ids: set[int]
                            ) -> list[ReflectionDraft]:
    """Parse the LLM's JSON array into ReflectionDrafts. Returns ``[]``
    on any parse failure — synthesizer errors are non-fatal.

    Filters ``source_fact_ids`` to only those that appear in
    ``valid_fact_ids`` (the set of facts we actually sent the model)
    so a hallucinated id doesn't survive into the reflections table.
    """
    cleaned = _strip_json_fence(text)
    start = cleaned.find("[")
    if start == -1:
        return []
    try:
        data, _ = json.JSONDecoder().raw_decode(cleaned[start:])
    except (json.JSONDecodeError, ValueError):
        return []
    if not isinstance(data, list):
        return []

    out: list[ReflectionDraft] = []
    for raw in data:
        if not isinstance(raw, dict):
            continue
        summary = (raw.get("summary") or "").strip()
        if not summary:
            continue
        raw_ids = raw.get("source_fact_ids") or []
        if not isinstance(raw_ids, list):
            continue
        clean_ids: list[int] = []
        for v in raw_ids:
            try:
                vi = int(v)
            except (TypeError, ValueError):
                continue
            if vi in valid_fact_ids:
                clean_ids.append(vi)
        # ≥2 valid grounding facts; otherwise this is just a single
        # observation, not a reflection.
        if len(clean_ids) < 2:
            continue
        entity = raw.get("entity")
        if isinstance(entity, str):
            entity = entity.strip() or None
        else:
            entity = None
        relation_type = raw.get("relation_type")
        if isinstance(relation_type, str):
            relation_type = relation_type.strip() or None
        else:
            relation_type = None
        out.append(ReflectionDraft(
            summary=summary[:200],  # hard cap so a runaway model can't blow row size
            source_fact_ids=clean_ids,
            entity=entity,
            relation_type=relation_type,
        ))
    return out


def format_facts_block(facts: Sequence[tuple]) -> str:
    """Render facts as ``<id>. <summary>`` lines for the prompt.

    Accepts the (id, summary, ...) shape returned by db helpers; only
    the first two fields are read so the same formatter works against
    different list shapes (event_logs rows, fts rows, etc.).
    """
    lines = []
    for row in facts:
        if not row:
            continue
        eid = row[0]
        summary = row[1] or ""
        if len(summary) > 200:
            summary = summary[:199] + "…"
        lines.append(f"{eid}. {summary}")
    return "\n".join(lines)


async def synthesize_reflections(
    llm_client,
    facts: Sequence[tuple],
    *,
    timeout_s: float = 20.0,
) -> list[ReflectionDraft]:
    """Call the LLM and parse its reflection output.

    Args:
        llm_client: any object with a ``chat(LLMRequest) -> resp`` /
            ``a_chat(...)`` method. The plugin passes its auditor
            client chain.
        facts: list of fact tuples, each at least ``(id, summary, ...)``.
            Must have ≥ 2 entries or this returns ``[]`` without
            an LLM call (no signal to synthesize from).
        timeout_s: per-call ceiling. On timeout returns ``[]``;
            never raises into the caller.

    Returns: list of validated ReflectionDrafts (≥2 source ids each).

    Pure-ish: doesn't touch the DB. The reconciler is responsible for
    persisting these and flipping ``absorbed=1`` on the source facts.
    """
    if not facts or len(facts) < 2 or llm_client is None:
        return []

    valid_ids = {row[0] for row in facts if row}
    block = format_facts_block(facts)
    prompt = SYNTHESIS_PROMPT.replace("{facts_block}", block)

    # Build the LLMRequest using whatever shape the host supports. We
    # try the rich Prompt API first (matches the auditor path) and
    # fall back to a plain-text chat call.
    try:
        # Imports are local so this module remains importable in test
        # harnesses where core.* isn't available — synthesize() simply
        # won't be reachable without a host context anyway.
        from core.provider import LLMRequest  # type: ignore
        from core.prompt_manager import Prompt  # type: ignore
        req = LLMRequest(
            system_prompt=[
                Prompt(
                    "You synthesize stable user patterns into one-sentence "
                    "reflections. Strict JSON output only.",
                    name="reflection_system",
                    source="kiraos",
                )
            ],
            user_prompt=[
                Prompt(prompt, name="reflection_user", source="kiraos")
            ],
        )
        req.assemble_prompt()
        coro = llm_client.chat(req)
    except Exception:
        # Fall back: try a plain str-prompt call. Some test stubs
        # only implement this shape.
        chat = getattr(llm_client, "chat", None) or getattr(llm_client, "a_chat", None)
        if chat is None:
            return []
        try:
            coro = chat(prompt)
        except Exception:
            return []

    try:
        resp = await asyncio.wait_for(coro, timeout=timeout_s)
    except asyncio.TimeoutError:
        logger.warning("[reflection] LLM call timed out; skipping synthesis")
        return []
    except Exception as exc:
        logger.warning(f"[reflection] LLM call failed: {exc}")
        return []

    # Response shape: try .text_response first (matches the auditor's
    # LLMResponse), then fall back to str(resp).
    text = getattr(resp, "text_response", None)
    if text is None:
        text = str(resp) if resp is not None else ""
    text = (text or "").strip()
    if not text:
        return []
    return parse_reflection_output(text, valid_ids)
