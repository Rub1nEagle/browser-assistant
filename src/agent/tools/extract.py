from __future__ import annotations

from playwright.async_api import Page

from ..llm.base import LLMClient
from ..llm.types import Message, TextBlock


# Hard cap on the snapshot we feed the extractor. ~25k tokens worst case;
# real pages are usually much smaller. Long pages get tail-truncated with
# a marker so the model knows the cut happened.
_MAX_SNAPSHOT_CHARS = 80_000


async def _page_snapshot(page: Page) -> str:
    """Read-only snapshot for the extractor. Default mode (no [ref=eN]
    noise) is cleaner for reading; it's a pure YAML view of the a11y tree."""
    text = await page.aria_snapshot()
    if len(text) <= _MAX_SNAPSHOT_CHARS:
        return text
    return text[:_MAX_SNAPSHOT_CHARS] + "\n…(snapshot truncated)"


async def run_extract(
    *,
    llm: LLMClient,
    page: Page,
    instruction: str,
    extractor_system: str,
    max_tokens: int = 2048,
) -> str:
    snapshot = await _page_snapshot(page)
    title = await page.title()
    user_text = (
        f"# Instruction\n{instruction.strip()}\n\n"
        f"# Page\nURL: {page.url}\nTitle: {title}\n\n"
        f"## Snapshot (accessibility tree, YAML)\n{snapshot}"
    )
    response = await llm.step(
        system=extractor_system,
        messages=[Message(role="user", blocks=[TextBlock(text=user_text)])],
        tools=[],
        max_tokens=max_tokens,
    )
    return response.text or "(extractor returned no text)"
