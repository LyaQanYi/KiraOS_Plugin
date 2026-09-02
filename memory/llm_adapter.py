"""LLM client adapter bridging hippocampus extraction to KiraAI's LLMModelClient.

Hippocampus uses messages-list style:
    await client.chat([{"role": "user", "content": ...}]) -> obj.text_response

KiraAI uses LLMRequest:
    await client.chat(LLMRequest(messages=[...])) -> LLMResponse.text_response

This adapter bridges the two so the extractor can stay provider-agnostic.
"""

from typing import Optional

from core.provider import LLMRequest, LLMModelClient
from core.prompt_manager import Prompt
from core.logging_manager import get_logger

logger = get_logger("kiraos_memory_llm", "cyan")


async def chat_text(
    client: LLMModelClient,
    prompt: str,
    *,
    system: Optional[str] = None,
) -> str:
    """Call an LLMModelClient with a single user prompt, return the text response.

    Returns `""` when no client is wired. Provider exceptions propagate to the
    caller: every call site sits inside an `asyncio.wait_for(...)` with its own
    `except` that names the operation ("Merge facts", "Reflection generation",
    ...) and degrades accordingly. Swallowing them here collapsed all eight
    operations into one anonymous `chat_text failed:` line and made those
    handlers' non-timeout branches unreachable.
    """
    if client is None:
        logger.warning("chat_text called with client=None")
        return ""

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    req = LLMRequest(messages=messages)
    resp = await client.chat(req)

    text = getattr(resp, "text_response", "") or ""
    return text.strip()


def append_to_prompt_section(system_prompts, name: str, addition: str) -> bool:
    """Append `addition` to the system prompt section identified by `name`.

    Returns True if the section existed.
    """
    for p in system_prompts:
        if isinstance(p, Prompt) and p.name == name:
            p.content = (p.content or "") + addition
            return True
    return False
