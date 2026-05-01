"""Chunking strategy for long-page extraction."""
from __future__ import annotations

import asyncio

import pytest

from agent.tools.extract import _NONE_TOKEN, _chunk, run_extract


# --- chunker -----------------------------------------------------------------


def test_short_text_is_one_chunk():
    assert _chunk("hello", max_chars=100) == ["hello"]


def test_chunks_cover_whole_text_without_loss():
    text = "x" * 1_000
    chunks = _chunk(text, max_chars=300, max_chunks=10)
    assert "".join(chunks) == text
    assert all(len(c) <= 300 + 1 for c in chunks)  # ceil rounding fudge


def test_chunks_capped_at_max_chunks():
    """Pages that would need more than max_chunks get sampled, not chopped
    off. Total characters returned equals the full text length (no slack)."""
    text = "y" * 10_000
    chunks = _chunk(text, max_chars=500, max_chunks=4)
    assert len(chunks) == 4
    assert "".join(chunks) == text


# --- map-reduce orchestration ------------------------------------------------


class _ScriptedLLM:
    """Records every prompt and returns scripted replies in order."""

    model = "test"

    def __init__(self, replies: list[str]):
        self._replies = list(replies)
        self.prompts: list[str] = []

    async def step(self, *, system, messages, tools, max_tokens=2048):
        # Pull the user-text content off the single user message we send.
        user_text = ""
        for m in messages:
            if m.role == "user":
                for b in m.blocks:
                    user_text += getattr(b, "text", "")
        self.prompts.append(user_text)
        text = self._replies.pop(0) if self._replies else ""
        from agent.llm.types import StepResponse, TextBlock, Usage
        return StepResponse(
            blocks=[TextBlock(text=text)] if text else [],
            stop_reason="end_turn",
            usage=Usage(),
        )

    def estimate_cost_usd(self, **_):  # noqa: D401 — match the ABC
        return 0.0


class _StubPage:
    def __init__(self, snapshot: str, url="https://x", title="X"):
        self._snapshot = snapshot
        self.url = url
        self._title = title

    async def aria_snapshot(self, *args, **kwargs):
        return self._snapshot

    async def title(self):
        return self._title


@pytest.mark.asyncio
async def test_short_page_uses_single_call():
    llm = _ScriptedLLM(replies=["one shot answer"])
    out = await run_extract(
        llm=llm,
        page=_StubPage("- short snapshot"),
        instruction="say hi",
        extractor_system="SYS",
    )
    assert out == "one shot answer"
    assert len(llm.prompts) == 1
    # Single-call prompt should NOT mention chunking.
    assert "chunk" not in llm.prompts[0].lower()


@pytest.mark.asyncio
async def test_long_page_runs_map_reduce():
    """Long page → N map calls + 1 reducer. Reducer receives only the
    non-NONE partials and emits the merged answer."""
    big = "line\n" * 20_000  # ~100k chars → 4 chunks at default settings
    n_chunks = len(_chunk(big))
    assert n_chunks > 1, "test premise: page must split"

    # One reply per chunk (alternating partial/NONE) + one reply for the reducer.
    replies = []
    for i in range(n_chunks):
        replies.append(f"partial-{i}" if i % 2 == 0 else _NONE_TOKEN)
    replies.append("merged")

    llm = _ScriptedLLM(replies=replies)
    out = await run_extract(
        llm=llm,
        page=_StubPage(big),
        instruction="extract emails",
        extractor_system="SYS",
    )

    assert len(llm.prompts) == n_chunks + 1  # map + reduce
    reducer_prompt = llm.prompts[-1]
    # All non-NONE partials made it into the reducer's input.
    for i in range(0, n_chunks, 2):
        assert f"partial-{i}" in reducer_prompt
    assert out == "merged"


@pytest.mark.asyncio
async def test_long_page_with_only_none_returns_explanatory_message():
    n_chunks_min = 3  # any plausible big text triggers multiple chunks
    llm = _ScriptedLLM(replies=[_NONE_TOKEN] * (n_chunks_min + 5))
    big = "z" * 200_000
    out = await run_extract(
        llm=llm,
        page=_StubPage(big),
        instruction="extract spaceships",
        extractor_system="SYS",
    )
    assert "no relevant data" in out.lower()
