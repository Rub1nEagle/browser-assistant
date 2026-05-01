from __future__ import annotations

import math

from playwright.async_api import Page

from ..llm.base import LLMClient
from ..llm.types import Message, TextBlock


# Per-chunk character budget. ~30k chars ≈ 7-8k tokens, leaves headroom
# for the user prompt and instruction inside the extractor's context.
_CHUNK_SIZE_CHARS = 30_000

# Hard cap so a 1MB SPA snapshot doesn't translate into 30 LLM calls.
# 6 × 30k = 180k chars of effective coverage, which is enough for any
# real page we've seen — beyond that we sample evenly.
_MAX_CHUNKS = 6

# Sentinel the extractor returns when its chunk has nothing relevant.
_NONE_TOKEN = "NONE"


def _chunk(text: str, *, max_chars: int = _CHUNK_SIZE_CHARS,
           max_chunks: int = _MAX_CHUNKS) -> list[str]:
    """Split a snapshot for map-reduce extraction.

    For pages that fit in `max_chars` we return one chunk verbatim. For
    longer pages we split into N evenly-sized pieces (no overlap) and cap
    at `max_chunks` — pages bigger than `max_chunks * max_chars` get
    sampled rather than fully scanned. Sampling beats truncation: the
    tail of the page is no less likely to hold the answer than the head.
    """
    if len(text) <= max_chars:
        return [text]
    n = min(max_chunks, math.ceil(len(text) / max_chars))
    chunk_size = math.ceil(len(text) / n)
    return [text[i * chunk_size:(i + 1) * chunk_size] for i in range(n)]


async def _extract_one(
    llm: LLMClient,
    *,
    extractor_system: str,
    instruction: str,
    url: str,
    title: str,
    snapshot: str,
    max_tokens: int,
    chunk_idx: int | None = None,
    total_chunks: int | None = None,
) -> str:
    chunk_note = ""
    if chunk_idx is not None and total_chunks is not None:
        chunk_note = (
            f"\nThis is chunk {chunk_idx} of {total_chunks} from a long page. "
            f"If the data the instruction asks for is not in *this* chunk, "
            f"reply with the literal word {_NONE_TOKEN} and nothing else. "
            f"A reducer will combine answers from all chunks afterwards.\n"
        )
    user_text = (
        f"# Instruction\n{instruction.strip()}\n"
        f"{chunk_note}"
        f"\n# Page\nURL: {url}\nTitle: {title}\n\n"
        f"## Snapshot (accessibility tree, YAML)\n{snapshot}"
    )
    response = await llm.step(
        system=extractor_system,
        messages=[Message(role="user", blocks=[TextBlock(text=user_text)])],
        tools=[],
        max_tokens=max_tokens,
    )
    return response.text or "(extractor returned no text)"


async def _reduce(
    llm: LLMClient,
    *,
    extractor_system: str,
    instruction: str,
    partials: list[str],
    max_tokens: int,
) -> str:
    user_text = (
        "Combine these partial extractions from different sections of the "
        "same page into one deduplicated final answer that follows the "
        "instruction. Drop the chunk-K/N markers from your output. If items "
        "appear in more than one chunk, list each only once.\n\n"
        f"# Instruction\n{instruction.strip()}\n\n"
        f"# Partial extractions\n" + "\n\n---\n\n".join(partials)
    )
    response = await llm.step(
        system=extractor_system,
        messages=[Message(role="user", blocks=[TextBlock(text=user_text)])],
        tools=[],
        max_tokens=max_tokens,
    )
    return response.text or "(reducer returned no text)"


async def run_extract(
    *,
    llm: LLMClient,
    page: Page,
    instruction: str,
    extractor_system: str,
    max_tokens: int = 2048,
) -> str:
    """Extract structured data from the current page.

    Short pages → one LLM call (same as v1).
    Long pages  → map-reduce: each chunk is asked the same question
                  (with permission to reply NONE), then a reducer LLM
                  call merges the non-NONE partials into one answer.
    """
    snapshot = await page.aria_snapshot()
    title = await page.title()
    url = page.url

    chunks = _chunk(snapshot)
    if len(chunks) == 1:
        return await _extract_one(
            llm,
            extractor_system=extractor_system,
            instruction=instruction,
            url=url,
            title=title,
            snapshot=chunks[0],
            max_tokens=max_tokens,
        )

    partials: list[str] = []
    for i, chunk in enumerate(chunks, start=1):
        result = await _extract_one(
            llm,
            extractor_system=extractor_system,
            instruction=instruction,
            url=url,
            title=title,
            snapshot=chunk,
            max_tokens=max_tokens,
            chunk_idx=i,
            total_chunks=len(chunks),
        )
        if result.strip().upper() != _NONE_TOKEN:
            partials.append(f"[chunk {i}/{len(chunks)}]\n{result}")

    if not partials:
        return "(extractor: no relevant data found across chunks)"
    if len(partials) == 1:
        # Strip the marker for parity with single-chunk runs.
        return partials[0].split("\n", 1)[1]

    return await _reduce(
        llm,
        extractor_system=extractor_system,
        instruction=instruction,
        partials=partials,
        max_tokens=max_tokens,
    )
