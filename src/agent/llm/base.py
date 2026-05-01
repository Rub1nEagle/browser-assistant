from __future__ import annotations

from abc import ABC, abstractmethod

from .types import Message, StepResponse, Tool


class LLMClient(ABC):
    """Provider-neutral interface for the planner LLM.

    Implementations translate provider-specific tool-call formats into the
    shared Block/Message types so the rest of the agent can stay agnostic.
    """

    model: str

    @abstractmethod
    async def step(
        self,
        *,
        system: str,
        messages: list[Message],
        tools: list[Tool],
        max_tokens: int = 4096,
    ) -> StepResponse: ...

    @abstractmethod
    def estimate_cost_usd(
        self,
        *,
        input_tokens: int,
        output_tokens: int,
        cache_read_tokens: int = 0,
        cache_creation_tokens: int = 0,
    ) -> float | None:
        """Return USD cost for the step, or `None` if pricing for this
        model is unknown. Callers MUST treat `None` as 'unknown', not as
        zero — otherwise MAX_COST_USD becomes a silent no-op."""
        ...
