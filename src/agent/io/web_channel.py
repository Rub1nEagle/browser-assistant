from __future__ import annotations

import asyncio

from ..events import EventBus, NeedsUserInput
from .channel import IOChannel


class WebChannel(IOChannel):
    """`ask_user` over a server-side future.

    The agent calls `ask(question)` from inside a tool handler. We emit a
    `NeedsUserInput` event (the WS endpoint forwards it to the browser)
    and suspend until the WS endpoint calls `deliver_answer(text)`.

    A single in-flight question at a time is fine — the agent loop is
    serial, so we can't get two pending asks.
    """

    def __init__(self, bus: EventBus):
        self._bus = bus
        self._pending: asyncio.Future[str] | None = None

    async def ask(self, question: str) -> str:
        loop = asyncio.get_running_loop()
        if self._pending is not None and not self._pending.done():
            return "(internal error: another ask_user is already pending)"
        self._pending = loop.create_future()
        await self._bus.emit(NeedsUserInput(question=question))
        try:
            return await self._pending
        finally:
            self._pending = None

    def deliver_answer(self, answer: str) -> bool:
        """Called by the WebSocket endpoint when the user replies.
        Returns True if a pending ask was waiting, False otherwise."""
        if self._pending is None or self._pending.done():
            return False
        self._pending.set_result(answer)
        return True

    def cancel(self, reason: str = "cancelled") -> None:
        """Wake up a hanging ask if the run is aborted."""
        if self._pending is not None and not self._pending.done():
            self._pending.set_result(f"(no answer: {reason})")
