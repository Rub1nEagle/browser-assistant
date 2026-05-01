from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from .browser.controller import BrowserController
from .browser.telemetry import format_reflection, take_snapshot
from .config import Settings
from .context.manager import ContextManager
from .events import (
    AgentStarted,
    AgentThinking,
    EventBus,
    LLMRequestCompleted,
    LLMRequestStarted,
    ScratchpadUpdated,
    TaskCompleted,
    TaskFailed,
    ToolCallCompleted,
    ToolCallStarted,
)
from .io.channel import IOChannel, StdinChannel
from .llm.base import LLMClient
from .llm.types import ToolResultBlock, ToolUseBlock
from .tools.registry import ToolContext, ToolRegistry, build_registry


# Tools whose successful execution is expected to change the page. After
# each such call we capture pre/post telemetry and append a reflection
# note when the page looks unchanged.
_MUTATING_TOOLS = frozenset({
    "navigate", "click", "type", "press_key", "select", "go_back", "go_forward",
})

# Repeated-action detector: how many identical (name, args) calls in a row
# before we inject a system note.
_REPEAT_THRESHOLD = 3


def _summarise_result(content: str, limit: int = 160) -> str:
    one_line = " ".join(content.split())
    return one_line if len(one_line) <= limit else one_line[: limit - 1] + "…"


def _call_signature(call: ToolUseBlock) -> tuple[str, str]:
    try:
        args = json.dumps(call.input, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError):
        args = repr(call.input)
    return (call.name, args)


@dataclass
class AgentRun:
    final_report: str | None = None
    failed_reason: str | None = None
    steps: int = 0
    cost_usd: float = 0.0
    # True if at least one step couldn't be priced — sum is partial.
    cost_partial: bool = False


@dataclass
class Agent:
    llm: LLMClient
    registry: ToolRegistry
    tool_ctx: ToolContext
    bus: EventBus
    settings: Settings
    context: ContextManager
    _recent_calls: deque[tuple[str, str]] = field(
        default_factory=lambda: deque(maxlen=_REPEAT_THRESHOLD)
    )

    async def run(self, task: str) -> AgentRun:
        await self.bus.emit(AgentStarted(task=task))
        self.context.add_user_text(task)

        run = AgentRun()

        for step in range(1, self.settings.max_steps + 1):
            run.steps = step
            await self.bus.emit(LLMRequestStarted(step=step))
            response = await self.llm.step(
                system=self.context.build_system(),
                messages=self.context.build_messages(),
                tools=self.registry.tools(),
            )
            cost = self.llm.estimate_cost_usd(
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
                cache_read_tokens=response.usage.cache_read_tokens,
                cache_creation_tokens=response.usage.cache_creation_tokens,
            )
            if cost is None:
                run.cost_partial = True
            else:
                run.cost_usd += cost
            await self.bus.emit(LLMRequestCompleted(
                step=step,
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
                cache_read_tokens=response.usage.cache_read_tokens,
                cache_creation_tokens=response.usage.cache_creation_tokens,
                cost_usd=cost,
            ))

            if response.text:
                await self.bus.emit(AgentThinking(text=response.text))

            self.context.record_assistant(list(response.blocks))

            tool_calls = response.tool_calls
            if not tool_calls:
                run.final_report = response.text or "(no report)"
                await self.bus.emit(TaskCompleted(report=run.final_report))
                return run

            result_blocks: list[ToolResultBlock] = []
            terminal_report: str | None = None
            for call in tool_calls:
                self._recent_calls.append(_call_signature(call))
                await self.bus.emit(ToolCallStarted(tool=call.name, args=call.input))

                pre = None
                if call.name in _MUTATING_TOOLS:
                    pre = await take_snapshot(self.tool_ctx.controller.page)

                result = await self.registry.dispatch(call.name, call.input, self.tool_ctx)

                content = result.content
                if call.name in _MUTATING_TOOLS and not result.is_error and pre is not None:
                    post = await take_snapshot(self.tool_ctx.controller.page)
                    note = format_reflection(pre, post)
                    if note:
                        content = f"{content}\n\n{note}"

                await self.bus.emit(ToolCallCompleted(
                    tool=call.name,
                    args=call.input,
                    result_summary=_summarise_result(content),
                    is_error=result.is_error,
                ))
                result_blocks.append(ToolResultBlock(
                    tool_use_id=call.id,
                    content=content,
                    is_error=result.is_error,
                ))
                if result.is_terminal and terminal_report is None:
                    terminal_report = result.final_report or result.content

            self._maybe_append_repeat_note(result_blocks)
            self.context.record_tool_results(result_blocks)
            await self.bus.emit(ScratchpadUpdated(entries=self.context.list_scratchpad()))

            if terminal_report is not None:
                run.final_report = terminal_report
                await self.bus.emit(TaskCompleted(report=terminal_report))
                return run

            if self.settings.max_cost_usd and run.cost_usd >= self.settings.max_cost_usd:
                run.failed_reason = (
                    f"cost cap of ${self.settings.max_cost_usd:.2f} reached "
                    f"(spent ${run.cost_usd:.2f}); stopping."
                )
                await self.bus.emit(TaskFailed(reason=run.failed_reason))
                return run

        run.failed_reason = f"step cap of {self.settings.max_steps} reached"
        await self.bus.emit(TaskFailed(reason=run.failed_reason))
        return run

    def _maybe_append_repeat_note(self, results: list[ToolResultBlock]) -> None:
        if len(self._recent_calls) < _REPEAT_THRESHOLD:
            return
        if len(set(self._recent_calls)) != 1:
            return
        if not results:
            return
        name, _ = self._recent_calls[-1]
        note = (
            f"\n\n[repeated-action detector] You have called {name} with the same "
            f"arguments {_REPEAT_THRESHOLD} times in a row. Either it is failing "
            "silently or you are stuck. Stop repeating: re-observe to see fresh "
            "state, try a different element/approach, ask_user, or done with an "
            "explanation."
        )
        # Append to the last result so the agent sees it together with what
        # it just got back.
        last = results[-1]
        results[-1] = ToolResultBlock(
            tool_use_id=last.tool_use_id,
            content=last.content + note,
            is_error=last.is_error,
        )
        # Reset so we don't keep nagging on the same streak.
        self._recent_calls.clear()


async def build_agent(
    *,
    settings: Settings,
    bus: EventBus,
    system_prompt: str,
    extractor_prompt: str,
    profile_dir: Path | None = None,
    channel: IOChannel | None = None,
) -> tuple[Agent, BrowserController]:
    """Wire up the agent with its dependencies. Caller owns the controller
    lifecycle (start/stop)."""

    if settings.provider == "anthropic":
        from .llm.anthropic_client import AnthropicClient
        llm: LLMClient = AnthropicClient(
            api_key=settings.api_key, model=settings.model, base_url=settings.base_url,
        )
    elif settings.provider == "openai":
        from .llm.openai_client import OpenAIClient
        llm = OpenAIClient(
            api_key=settings.api_key, model=settings.model, base_url=settings.base_url,
        )
    else:
        raise NotImplementedError(f"LLM provider '{settings.provider}' is not supported.")

    controller = BrowserController(profile_dir=profile_dir or settings.profile_dir)
    context = ContextManager(system_base=system_prompt)
    registry = build_registry()
    tool_ctx = ToolContext(
        controller=controller,
        context=context,
        llm=llm,
        channel=channel or StdinChannel(),
        extractor_system=extractor_prompt,
    )
    agent = Agent(
        llm=llm,
        registry=registry,
        tool_ctx=tool_ctx,
        bus=bus,
        settings=settings,
        context=context,
    )
    return agent, controller
