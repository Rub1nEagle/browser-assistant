"""Repeated-action detector — operates on whole turns, not individual calls."""
from __future__ import annotations

from agent.context.manager import ContextManager
from agent.core import Agent, _REPEAT_THRESHOLD, _call_signature
from agent.events import EventBus
from agent.llm.types import ToolResultBlock, ToolUseBlock


def _agent() -> Agent:
    """Construct an Agent we can poke at directly. None of the real
    deps are exercised — we only test the detector method."""
    return Agent(
        llm=None,        # type: ignore[arg-type]
        registry=None,   # type: ignore[arg-type]
        tool_ctx=None,   # type: ignore[arg-type]
        bus=EventBus(),
        settings=None,   # type: ignore[arg-type]
        context=ContextManager(system_base="x"),
    )


def _turn(*calls: ToolUseBlock) -> tuple[tuple[str, str], ...]:
    return tuple(_call_signature(c) for c in calls)


def _result_block(content: str = "ok") -> ToolResultBlock:
    return ToolResultBlock(tool_use_id="t1", content=content)


def test_no_note_below_threshold():
    agent = _agent()
    sig = _turn(ToolUseBlock(id="t1", name="click", input={"element_id": "e5"}))
    for _ in range(_REPEAT_THRESHOLD - 1):
        agent._recent_turns.append(sig)

    blocks = [_result_block()]
    agent._maybe_append_repeat_note(blocks)
    assert "repeated-action" not in blocks[0].content


def test_note_appears_after_three_identical_turns():
    agent = _agent()
    sig = _turn(ToolUseBlock(id="t1", name="click", input={"element_id": "e5"}))
    for _ in range(_REPEAT_THRESHOLD):
        agent._recent_turns.append(sig)

    blocks = [_result_block()]
    agent._maybe_append_repeat_note(blocks)
    assert "repeated-action detector" in blocks[0].content
    assert "click" in blocks[0].content
    # Streak resets after the warning so we don't nag again.
    assert len(agent._recent_turns) == 0


def test_note_skipped_when_a_turn_differs():
    agent = _agent()
    same = _turn(ToolUseBlock(id="t1", name="click", input={"element_id": "e5"}))
    different = _turn(ToolUseBlock(id="t1", name="click", input={"element_id": "e6"}))
    agent._recent_turns.append(same)
    agent._recent_turns.append(different)
    agent._recent_turns.append(same)

    blocks = [_result_block()]
    agent._maybe_append_repeat_note(blocks)
    assert "repeated-action" not in blocks[0].content


def test_legitimately_repeated_calls_within_one_turn_dont_trip_detector():
    """The OLD per-call detector would fire here on a single turn that
    happened to make the same tool call twice. The per-turn detector
    treats this as one entry — must NOT trigger."""
    agent = _agent()
    multi_call_turn = _turn(
        ToolUseBlock(id="t1", name="remember", input={"key": "k", "value": "v"}),
        ToolUseBlock(id="t2", name="observe", input={}),
    )
    agent._recent_turns.append(multi_call_turn)

    blocks = [_result_block(), _result_block()]
    agent._maybe_append_repeat_note(blocks)
    for b in blocks:
        assert "repeated-action" not in b.content


def test_signature_is_argument_aware():
    """Same tool name, different args → different signatures."""
    a = _call_signature(ToolUseBlock(id="t1", name="click", input={"element_id": "e1"}))
    b = _call_signature(ToolUseBlock(id="t1", name="click", input={"element_id": "e2"}))
    assert a != b


def test_signature_is_order_independent_for_dict_args():
    """JSON serialisation uses sort_keys=True so equivalent dicts hash the
    same regardless of which order the LLM happened to emit fields."""
    a = _call_signature(ToolUseBlock(id="t1", name="type",
                                     input={"element_id": "e1", "text": "hi"}))
    b = _call_signature(ToolUseBlock(id="t1", name="type",
                                     input={"text": "hi", "element_id": "e1"}))
    assert a == b
