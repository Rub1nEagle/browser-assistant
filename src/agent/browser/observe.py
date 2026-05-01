from __future__ import annotations

import re
from dataclasses import dataclass

from playwright.async_api import Page


# `[ref=e123]` markers in the AI-mode aria snapshot. Playwright resolves
# these via the `aria-ref=` selector engine.
_REF_PATTERN = re.compile(r"\[ref=(e\d+)\]")

# `<role> "<name>" [ref=eN]` — the typical line shape in the AI snapshot.
# We use it to derive a short human label per ref for the destructive-action
# guardrail, without forcing every consumer to re-parse the snapshot.
_LABEL_PATTERN = re.compile(r'([a-zA-Z][\w-]*)\s+"([^"]*)"\s+\[ref=(e\d+)\]')


@dataclass
class ObserveResult:
    url: str
    title: str
    rendered: str
    refs: set[str]
    # ref → "<role> '<name>'" derived from the snapshot. Best-effort —
    # elements without a quoted name simply don't appear here.
    labels: dict[str, str]


async def observe(page: Page, *, max_depth: int | None = 25) -> ObserveResult:
    """Capture the current page's accessibility snapshot in AI mode.

    Output is a YAML-style tree with `[ref=eN]` markers that the agent
    passes back to `click`/`type`. Each new observe replaces the prior
    ref set in Playwright; old refs return an error on use.
    """
    snapshot = await page.aria_snapshot(mode="ai", depth=max_depth)
    refs = set(_REF_PATTERN.findall(snapshot))
    labels = {
        ref: f'{role} "{name}"' for role, name, ref in _LABEL_PATTERN.findall(snapshot)
    }
    title = await page.title()
    rendered = (
        f"URL: {page.url}\n"
        f"Title: {title}\n\n"
        f"{snapshot}"
    )
    return ObserveResult(
        url=page.url, title=title, rendered=rendered, refs=refs, labels=labels,
    )
