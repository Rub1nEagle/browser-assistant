from __future__ import annotations

import asyncio
import sys
from abc import ABC, abstractmethod


class IOChannel(ABC):
    """Bidirectional channel for blocking interactions with the user.

    The agent calls `ask(question)` from inside an async tool handler;
    the implementation suspends until the user replies. CLI uses stdin
    via to_thread; the Phase 5 web UI will plug in a WebSocket round-trip
    behind the same interface.
    """

    @abstractmethod
    async def ask(self, question: str) -> str: ...


class StdinChannel(IOChannel):
    async def ask(self, question: str) -> str:
        # `input()` blocks the event loop; offload to a worker thread so
        # other async tasks (e.g. log timers, browser idle waits) keep running.
        prompt = f"\n[ask_user] {question}\n> "
        return await asyncio.to_thread(self._read, prompt)

    @staticmethod
    def _read(prompt: str) -> str:
        try:
            return input(prompt)
        except EOFError:
            # Headless / piped stdin: surface as an explicit error string
            # so the agent can `done` rather than crash.
            print("(stdin closed; cannot ask user)", file=sys.stderr)
            return "(no answer: stdin unavailable)"
