from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable


@dataclass
class Event:
    pass


@dataclass
class AgentStarted(Event):
    task: str


@dataclass
class LLMRequestStarted(Event):
    step: int


@dataclass
class LLMRequestCompleted(Event):
    step: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    # `None` means the model's pricing isn't in our table — distinct from
    # "$0.00" so the UI can render "?" and MAX_COST_USD doesn't silently
    # treat it as free.
    cost_usd: float | None = 0.0


@dataclass
class AgentThinking(Event):
    text: str


@dataclass
class ToolCallStarted(Event):
    tool: str
    args: dict[str, Any]


@dataclass
class ToolCallCompleted(Event):
    tool: str
    args: dict[str, Any]
    result_summary: str
    is_error: bool = False


@dataclass
class TaskCompleted(Event):
    report: str


@dataclass
class TaskFailed(Event):
    reason: str


@dataclass
class NeedsUserInput(Event):
    question: str


@dataclass
class ScratchpadUpdated(Event):
    entries: dict[str, str]


Listener = Callable[[Event], Awaitable[None] | None]


@dataclass
class EventBus:
    _listeners: list[Listener] = field(default_factory=list)

    def subscribe(self, listener: Listener) -> None:
        self._listeners.append(listener)

    async def emit(self, event: Event) -> None:
        for listener in self._listeners:
            result = listener(event)
            if hasattr(result, "__await__"):
                await result  # type: ignore[misc]
