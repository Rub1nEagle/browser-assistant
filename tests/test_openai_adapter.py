"""OpenAI-format Block ↔ message translation. Round-trips a small history
through both directions to catch drift between Anthropic-shaped Blocks
and OpenAI's flat tool-call format."""
from __future__ import annotations

import json
from types import SimpleNamespace

from agent.llm.openai_client import _decode_message, _messages_to_openai
from agent.llm.types import Message, TextBlock, ToolResultBlock, ToolUseBlock


def test_user_text_message_passes_through():
    msgs = _messages_to_openai([Message(role="user", blocks=[TextBlock(text="hi")])])
    assert msgs == [{"role": "user", "content": "hi"}]


def test_assistant_with_tool_call_serialises_correctly():
    history = [
        Message(role="assistant", blocks=[
            TextBlock(text="navigating"),
            ToolUseBlock(id="call_42", name="navigate", input={"url": "https://x"}),
        ]),
    ]
    out = _messages_to_openai(history)
    assert len(out) == 1
    msg = out[0]
    assert msg["role"] == "assistant"
    assert msg["content"] == "navigating"
    assert len(msg["tool_calls"]) == 1
    tc = msg["tool_calls"][0]
    assert tc["id"] == "call_42"
    assert tc["type"] == "function"
    assert tc["function"]["name"] == "navigate"
    # OpenAI expects arguments as a JSON string, not an object
    assert json.loads(tc["function"]["arguments"]) == {"url": "https://x"}


def test_tool_result_becomes_separate_role_tool_message():
    history = [
        Message(role="user", blocks=[
            ToolResultBlock(tool_use_id="call_42", content="navigated to https://x"),
        ]),
    ]
    out = _messages_to_openai(history)
    # Anthropic packs tool_results inside a user message; OpenAI splits them.
    tool_msgs = [m for m in out if m["role"] == "tool"]
    assert tool_msgs == [{
        "role": "tool",
        "tool_call_id": "call_42",
        "content": "navigated to https://x",
    }]


def test_decode_message_extracts_text_and_tool_calls():
    fake = SimpleNamespace(
        content="thinking out loud",
        tool_calls=[
            SimpleNamespace(
                id="call_7",
                function=SimpleNamespace(name="click", arguments='{"element_id": "e3"}'),
            ),
        ],
    )
    blocks = _decode_message(fake)
    text_blocks = [b for b in blocks if isinstance(b, TextBlock)]
    tool_blocks = [b for b in blocks if isinstance(b, ToolUseBlock)]
    assert text_blocks[0].text == "thinking out loud"
    assert tool_blocks[0].id == "call_7"
    assert tool_blocks[0].name == "click"
    assert tool_blocks[0].input == {"element_id": "e3"}


def test_decode_message_preserves_malformed_arguments():
    """When the model returns broken JSON, surface it via the
    `_unparsed_arguments` key rather than crashing — the agent loop can
    then recover instead of dying mid-run."""
    fake = SimpleNamespace(
        content=None,
        tool_calls=[
            SimpleNamespace(
                id="call_8",
                function=SimpleNamespace(name="navigate", arguments="{not-json"),
            ),
        ],
    )
    blocks = _decode_message(fake)
    tool_blocks = [b for b in blocks if isinstance(b, ToolUseBlock)]
    assert tool_blocks[0].input == {"_unparsed_arguments": "{not-json"}
