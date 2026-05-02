"""WebChannel: race-safe ask/deliver/cancel."""
from __future__ import annotations

import asyncio

import pytest

from agent.events import EventBus
from agent.io.web_channel import WebChannel


@pytest.mark.asyncio
async def test_ask_returns_delivered_answer():
    bus = EventBus()
    ch = WebChannel(bus)

    # Schedule the answer to arrive on the next tick.
    async def deliver_after_yield():
        await asyncio.sleep(0)
        ch.deliver_answer("42")

    asyncio.create_task(deliver_after_yield())
    answer = await ch.ask("what is the meaning of life?")
    assert answer == "42"


@pytest.mark.asyncio
async def test_cancel_unblocks_a_pending_ask():
    bus = EventBus()
    ch = WebChannel(bus)

    async def cancel_after_yield():
        await asyncio.sleep(0)
        ch.cancel("run ended")

    asyncio.create_task(cancel_after_yield())
    answer = await ch.ask("anything?")
    assert "no answer" in answer
    assert "run ended" in answer


def test_deliver_without_pending_returns_false():
    ch = WebChannel(EventBus())
    assert ch.deliver_answer("ignored") is False


def test_cancel_without_pending_is_a_noop():
    ch = WebChannel(EventBus())
    # Should not raise.
    ch.cancel("idle")


@pytest.mark.asyncio
async def test_double_resolution_is_silent():
    """deliver_answer racing cancel — second one must not crash, only the
    first set_result wins. Without the guards this would throw
    InvalidStateError out of whichever ran second."""
    bus = EventBus()
    ch = WebChannel(bus)

    fut: asyncio.Future[str] = asyncio.get_running_loop().create_future()

    async def waiter():
        # Pretend the agent is awaiting the answer.
        return await ch.ask("hello?")

    waiter_task = asyncio.create_task(waiter())
    await asyncio.sleep(0)  # give waiter a turn so _pending is set

    # Race: both methods reach set_result.
    delivered = ch.deliver_answer("real-answer")
    ch.cancel("late cancel")  # must be silent
    assert delivered is True

    answer = await waiter_task
    # Whoever won, deliver_answer ran first and set the future.
    assert answer == "real-answer"
    fut.cancel()  # silence "never awaited" warning
