"""Context compression + scratchpad rendering."""
from __future__ import annotations

from agent.context.manager import ContextManager
from agent.llm.types import TextBlock, ToolResultBlock, ToolUseBlock


def _build_history() -> ContextManager:
    cm = ContextManager(system_base="ROOT")
    cm.add_user_text("do the task")
    cm.record_assistant([
        TextBlock(text="looking around"),
        ToolUseBlock(id="t1", name="observe", input={}),
    ])
    cm.record_tool_results([
        ToolResultBlock(tool_use_id="t1", content="URL: https://a.com\nTitle: A\n[ref=e1] [ref=e2]"),
    ])
    cm.record_assistant([ToolUseBlock(id="t2", name="click", input={"element_id": "e2"})])
    cm.record_tool_results([ToolResultBlock(tool_use_id="t2", content="clicked e2")])
    cm.record_assistant([ToolUseBlock(id="t3", name="observe", input={})])
    cm.record_tool_results([
        ToolResultBlock(tool_use_id="t3", content="URL: https://b.com\nTitle: B\n[ref=e10]"),
    ])
    return cm


def test_only_latest_observe_keeps_full_content():
    cm = _build_history()
    msgs = cm.build_messages()

    # Find the two ToolResultBlocks for observe — first one (t1) should be
    # collapsed, second (t3) should remain verbatim.
    observe_blocks: list[ToolResultBlock] = []
    for m in msgs:
        for b in m.blocks:
            if isinstance(b, ToolResultBlock) and b.tool_use_id in {"t1", "t3"}:
                observe_blocks.append(b)
    assert len(observe_blocks) == 2
    by_id = {b.tool_use_id: b for b in observe_blocks}

    assert "earlier observe" in by_id["t1"].content.lower()
    assert "https://a.com" in by_id["t1"].content
    assert "https://b.com" in by_id["t3"].content
    assert "[ref=e10]" in by_id["t3"].content


def test_non_observe_results_are_not_compressed():
    cm = _build_history()
    msgs = cm.build_messages()
    click_block = None
    for m in msgs:
        for b in m.blocks:
            if isinstance(b, ToolResultBlock) and b.tool_use_id == "t2":
                click_block = b
    assert click_block is not None
    assert click_block.content == "clicked e2"


def test_scratchpad_renders_into_system_prompt():
    cm = ContextManager(system_base="BASE")
    assert cm.build_system() == "BASE"  # empty pad → unchanged

    cm.remember("vacancy", "https://hh.ru/v/123")
    cm.remember("notes", "пользователь хочет ML/Python")
    sys = cm.build_system()
    assert sys.startswith("BASE")
    assert "# Scratchpad" in sys
    assert "vacancy" in sys and "https://hh.ru/v/123" in sys
    assert "notes" in sys and "ML/Python" in sys


def test_scratchpad_recall_reads_what_remember_wrote():
    cm = ContextManager(system_base="BASE")
    cm.remember("k", "v")
    assert cm.recall("k") == "v"
    assert cm.recall("missing") is None
