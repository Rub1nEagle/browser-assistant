"""Reflection heuristic — silent on real change, vocal when nothing happened."""
from __future__ import annotations

from agent.browser.telemetry import TelemetrySnapshot, format_reflection


def _snap(url="https://a.com", title="A", body=1000):
    return TelemetrySnapshot(url=url, title=title, body_text_len=body)


def test_url_change_is_silent():
    pre = _snap()
    post = _snap(url="https://b.com")
    assert format_reflection(pre, post) is None


def test_title_change_is_silent():
    pre = _snap()
    post = _snap(title="A — search")
    assert format_reflection(pre, post) is None


def test_substantial_body_change_is_silent():
    pre = _snap(body=1000)
    post = _snap(body=1500)  # +50%, page clearly mutated
    assert format_reflection(pre, post) is None


def test_unchanged_page_emits_warning():
    pre = _snap(body=1000)
    post = _snap(body=1001)  # under the 1% threshold
    note = format_reflection(pre, post)
    assert note is not None
    assert "unchanged" in note.lower()


def test_missing_snapshot_is_silent():
    """If we couldn't capture pre or post (mid-navigation, etc.), skipping
    the reflection note is preferable to a false warning."""
    assert format_reflection(None, _snap()) is None
    assert format_reflection(_snap(), None) is None


def test_threshold_is_one_percent():
    """Sanity check around the boundary — 0.99% should still warn,
    1.01% should not."""
    pre = _snap(body=10_000)
    just_under = _snap(body=10_099)
    just_over = _snap(body=10_101)
    assert format_reflection(pre, just_under) is not None
    assert format_reflection(pre, just_over) is None
