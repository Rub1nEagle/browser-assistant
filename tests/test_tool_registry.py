"""Pydantic validation of tool args + destructive-action policy."""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from agent.context.manager import ContextManager
from agent.io.channel import IOChannel
from agent.tools.policy import detect_destructive, parse_confirmation
from agent.tools.registry import ToolContext, build_registry


class _StubChannel(IOChannel):
    """In-memory channel that returns scripted answers, in order."""

    def __init__(self, answers: list[str]):
        self._answers = list(answers)
        self.questions: list[str] = []

    async def ask(self, question: str) -> str:
        self.questions.append(question)
        if not self._answers:
            return "no"
        return self._answers.pop(0)


@dataclass
class _StubController:
    """Stand-in for BrowserController. We skip real Playwright; the handlers
    that get past the policy still need *something* to call into."""

    labels: dict[str, str]
    click_calls: list[str] = None  # type: ignore[assignment]

    def __post_init__(self):
        self.click_calls = []

    def label_for(self, ref: str) -> str:
        return self.labels.get(ref, "")

    async def click(self, ref: str) -> str:
        self.click_calls.append(ref)
        return f"clicked {ref}"


def _ctx(labels=None, channel=None, confirm=True) -> ToolContext:
    return ToolContext(
        controller=_StubController(labels=labels or {}),  # type: ignore[arg-type]
        context=ContextManager(system_base="x"),
        channel=channel,
        confirm_destructive=confirm,
    )


# --- Pydantic validation -------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_tool_returns_error():
    reg = build_registry()
    res = await reg.dispatch("teleport", {}, _ctx())
    assert res.is_error
    assert "unknown tool" in res.content


@pytest.mark.asyncio
async def test_missing_required_arg_returns_error():
    reg = build_registry()
    res = await reg.dispatch("navigate", {}, _ctx())
    assert res.is_error
    assert "url" in res.content
    assert "required" in res.content.lower()


@pytest.mark.asyncio
async def test_extra_arg_is_rejected():
    reg = build_registry()
    res = await reg.dispatch("navigate", {"url": "https://x", "extra": 1}, _ctx())
    assert res.is_error
    assert "extra" in res.content.lower()


@pytest.mark.asyncio
async def test_bad_enum_value_is_rejected():
    reg = build_registry()
    res = await reg.dispatch("scroll", {"direction": "sideways"}, _ctx())
    assert res.is_error
    assert "direction" in res.content


# --- Destructive policy unit -------------------------------------------------


def test_detect_destructive_matches_label_in_russian():
    assert detect_destructive(
        tool_name="click", args={"element_id": "e1"}, ref_label='button "Удалить"',
    ) == "удал"


def test_detect_destructive_skips_safe_label():
    assert detect_destructive(
        tool_name="click", args={"element_id": "e1"}, ref_label='link "Home"',
    ) is None


def test_typing_without_submit_is_not_destructive():
    assert detect_destructive(
        tool_name="type",
        args={"element_id": "e1", "text": "delete this", "submit": False},
        ref_label="textbox",
    ) is None


def test_typing_with_submit_checks_text():
    assert detect_destructive(
        tool_name="type",
        args={"element_id": "e1", "text": "delete account", "submit": True},
        ref_label="textbox",
    ) == "delete"


def test_observe_is_never_destructive():
    assert detect_destructive(tool_name="observe", args={}, ref_label="") is None


def test_parse_confirmation_classifies_replies():
    assert parse_confirmation("yes") == "yes"
    assert parse_confirmation("ДА") == "yes"
    assert parse_confirmation("always") == "always"
    assert parse_confirmation("all") == "always"
    assert parse_confirmation("no") == "no"
    assert parse_confirmation("") == "no"
    assert parse_confirmation("ладно") == "no"  # unknown → no


# --- Destructive policy integration with dispatch ----------------------------


@pytest.mark.asyncio
async def test_destructive_click_blocks_when_user_says_no():
    reg = build_registry()
    ch = _StubChannel(["no"])
    ctx = _ctx(labels={"e5": 'button "Удалить"'}, channel=ch)

    res = await reg.dispatch("click", {"element_id": "e5"}, ctx)
    assert res.is_error
    assert "cancelled" in res.content
    assert any("удал" in q for q in ch.questions)


@pytest.mark.asyncio
async def test_destructive_click_proceeds_on_yes_then_calls_handler():
    reg = build_registry()
    ch = _StubChannel(["yes"])
    ctrl = _StubController(labels={"e5": 'button "Pay"'})
    ctx = ToolContext(
        controller=ctrl,  # type: ignore[arg-type]
        context=ContextManager(system_base="x"),
        channel=ch,
    )

    res = await reg.dispatch("click", {"element_id": "e5"}, ctx)
    assert not res.is_error
    assert ctrl.click_calls == ["e5"]
    assert len(ch.questions) == 1


@pytest.mark.asyncio
async def test_always_caches_pattern_and_skips_subsequent_prompts():
    reg = build_registry()
    ch = _StubChannel(["always"])  # only one answer to consume
    ctrl = _StubController(labels={"e5": 'button "Удалить"', "e6": 'button "Удалить"'})
    ctx = ToolContext(
        controller=ctrl,  # type: ignore[arg-type]
        context=ContextManager(system_base="x"),
        channel=ch,
    )

    # First call asks; second call should NOT ask (only one scripted answer).
    await reg.dispatch("click", {"element_id": "e5"}, ctx)
    await reg.dispatch("click", {"element_id": "e6"}, ctx)
    assert len(ch.questions) == 1
    assert "удал" in ctx.confirm_allowed_patterns
    assert ctrl.click_calls == ["e5", "e6"]


@pytest.mark.asyncio
async def test_disabled_policy_skips_confirmation():
    reg = build_registry()
    ch = _StubChannel([])
    ctrl = _StubController(labels={"e5": 'button "Удалить"'})
    ctx = ToolContext(
        controller=ctrl,  # type: ignore[arg-type]
        context=ContextManager(system_base="x"),
        channel=ch,
        confirm_destructive=False,
    )

    res = await reg.dispatch("click", {"element_id": "e5"}, ctx)
    # Channel never asked, click handler ran straight through.
    assert ch.questions == []
    assert not res.is_error
    assert ctrl.click_calls == ["e5"]
