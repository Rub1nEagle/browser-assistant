from __future__ import annotations

from dataclasses import dataclass

from playwright.async_api import Error as PlaywrightError, Page


@dataclass(frozen=True)
class TelemetrySnapshot:
    url: str
    title: str
    body_text_len: int


# Body-text length deltas under this fraction are treated as "page didn't
# really change" — the trigger for a reflection note. 1% catches typical
# silent click failures while ignoring noise like timestamp updates.
_NO_CHANGE_THRESHOLD = 0.01


async def take_snapshot(page: Page) -> TelemetrySnapshot | None:
    """Cheap pre/post-action telemetry. Returns None on transient errors
    (mid-navigation, frame detached) so the caller can skip reflection
    rather than crash the loop."""
    try:
        url = page.url
        title = await page.title()
        text_len = await page.evaluate(
            "() => document.body ? document.body.innerText.length : 0"
        )
        return TelemetrySnapshot(url=url, title=title, body_text_len=int(text_len or 0))
    except PlaywrightError:
        return None


def format_reflection(
    pre: TelemetrySnapshot | None, post: TelemetrySnapshot | None
) -> str | None:
    """Return a warning string when the action plausibly had no effect.
    Return None when there's clear evidence of change or no usable data."""
    if pre is None or post is None:
        return None
    if post.url != pre.url:
        return None
    if post.title != pre.title:
        return None
    base = max(pre.body_text_len, 1)
    diff = abs(post.body_text_len - pre.body_text_len)
    if diff / base >= _NO_CHANGE_THRESHOLD:
        return None
    return (
        "[reflection] Page looks unchanged after this action "
        f"(URL still {post.url}; body text length {post.body_text_len}, was "
        f"{pre.body_text_len}). The action may not have taken effect — "
        "re-observe and try a different element or approach if so."
    )
