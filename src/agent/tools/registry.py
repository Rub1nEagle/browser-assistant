from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from pydantic import BaseModel, ValidationError

from ..browser.controller import BrowserController, BrowserError, StaleElementError
from ..context.manager import ContextManager
from ..io.channel import IOChannel
from ..llm.base import LLMClient
from ..llm.types import Tool
from .extract import run_extract
from .policy import detect_destructive, parse_confirmation
from .schemas import (
    AskUserArgs,
    ClickArgs,
    DoneArgs,
    ExtractArgs,
    GoBackArgs,
    GoForwardArgs,
    NavigateArgs,
    ObserveArgs,
    PressKeyArgs,
    RecallArgs,
    RememberArgs,
    ScrollArgs,
    SelectArgs,
    TypeArgs,
    WaitForArgs,
    to_tool_schema,
)


@dataclass
class ToolContext:
    controller: BrowserController
    context: ContextManager
    llm: Optional[LLMClient] = None
    channel: Optional[IOChannel] = None
    extractor_system: str = ""
    confirm_destructive: bool = True
    # Patterns the user has approved with "always" for the rest of the run —
    # we don't ask twice for the same kind of action.
    confirm_allowed_patterns: set[str] = field(default_factory=set)


@dataclass
class ToolResult:
    content: str
    is_error: bool = False
    is_terminal: bool = False
    final_report: str | None = None


Handler = Callable[..., Awaitable[ToolResult]]


@dataclass
class ToolSpec:
    tool: Tool
    handler: Handler
    arg_model: type[BaseModel]


@dataclass
class ToolRegistry:
    specs: dict[str, ToolSpec] = field(default_factory=dict)

    def register(
        self, *, name: str, description: str, arg_model: type[BaseModel], handler: Handler
    ) -> None:
        tool = Tool(name=name, description=description, input_schema=to_tool_schema(arg_model))
        self.specs[name] = ToolSpec(tool=tool, handler=handler, arg_model=arg_model)

    def tools(self) -> list[Tool]:
        return [s.tool for s in self.specs.values()]

    async def dispatch(self, name: str, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        spec = self.specs.get(name)
        if spec is None:
            return ToolResult(content=f"unknown tool: {name}", is_error=True)
        try:
            validated = spec.arg_model.model_validate(args)
        except ValidationError as e:
            return ToolResult(
                content=f"invalid arguments for {name}: {_format_validation_error(e)}",
                is_error=True,
            )
        validated_args = validated.model_dump()

        # Destructive-action gate. The agent's prompt already nudges it to
        # stop before irreversible actions; this is the substring belt to
        # the prompt's suspenders.
        gate = await _confirm_if_destructive(name, validated_args, ctx)
        if gate is not None:
            return gate

        try:
            return await spec.handler(ctx, **validated_args)
        except StaleElementError as e:
            return ToolResult(content=str(e), is_error=True)
        except BrowserError as e:
            return ToolResult(content=f"browser error: {e}", is_error=True)


async def _confirm_if_destructive(
    name: str, args: dict[str, Any], ctx: ToolContext
) -> ToolResult | None:
    """If the call looks destructive, ask the user; on no/timeout, return
    a tool_result error so the agent sees the cancellation in-band."""
    if not ctx.confirm_destructive or ctx.channel is None:
        return None

    label = ""
    if "element_id" in args:
        label = ctx.controller.label_for(args["element_id"])
    matched = detect_destructive(tool_name=name, args=args, ref_label=label)
    if matched is None or matched in ctx.confirm_allowed_patterns:
        return None

    args_pretty = json.dumps(args, ensure_ascii=False)
    target = f"{label}" if label else f"ref {args.get('element_id', '<no element>')}"
    question = (
        f"Confirmation needed: {name}({args_pretty}) on {target} matched "
        f"destructive pattern '{matched}'. "
        "Reply 'yes' to allow once, 'always' to allow this kind for the "
        "rest of the run, anything else to cancel."
    )
    answer = await ctx.channel.ask(question)
    decision = parse_confirmation(answer)
    if decision == "always":
        ctx.confirm_allowed_patterns.add(matched)
        return None
    if decision == "yes":
        return None
    return ToolResult(
        content=(
            f"action cancelled by user: {name} matched destructive pattern "
            f"'{matched}'. Pick a different element or call done with an explanation."
        ),
        is_error=True,
    )


def _format_validation_error(err: ValidationError) -> str:
    """Compact error suitable to feed back to the LLM. We strip Pydantic's
    URL hints and stack noise — the agent just needs to know which field
    is wrong and why."""
    parts = []
    for e in err.errors(include_url=False):
        loc = ".".join(str(x) for x in e["loc"]) or "(root)"
        parts.append(f"{loc}: {e['msg']}")
    return "; ".join(parts)


# --- Tool handlers ---------------------------------------------------------


async def _navigate(ctx: ToolContext, url: str) -> ToolResult:
    return ToolResult(content=await ctx.controller.navigate(url))


async def _observe(ctx: ToolContext) -> ToolResult:
    result = await ctx.controller.observe()
    return ToolResult(content=result.rendered)


async def _click(ctx: ToolContext, element_id: str) -> ToolResult:
    return ToolResult(content=await ctx.controller.click(element_id))


async def _type(ctx: ToolContext, element_id: str, text: str, submit: bool = False) -> ToolResult:
    return ToolResult(content=await ctx.controller.type_text(element_id, text, submit=submit))


async def _press_key(ctx: ToolContext, element_id: str, key: str) -> ToolResult:
    return ToolResult(content=await ctx.controller.press_key(element_id, key))


async def _select(ctx: ToolContext, element_id: str, value: str) -> ToolResult:
    return ToolResult(content=await ctx.controller.select_option(element_id, value))


async def _scroll(ctx: ToolContext, direction: str, amount: int = 800) -> ToolResult:
    return ToolResult(content=await ctx.controller.scroll(direction, amount=amount))


async def _wait_for(
    ctx: ToolContext,
    condition: str,
    value: str | None = None,
    timeout_seconds: float = 10.0,
) -> ToolResult:
    return ToolResult(content=await ctx.controller.wait_for(
        condition, value=value, timeout_ms=int(timeout_seconds * 1000),
    ))


async def _go_back(ctx: ToolContext) -> ToolResult:
    return ToolResult(content=await ctx.controller.go_back())


async def _go_forward(ctx: ToolContext) -> ToolResult:
    return ToolResult(content=await ctx.controller.go_forward())


async def _extract(ctx: ToolContext, instruction: str) -> ToolResult:
    if ctx.llm is None:
        return ToolResult(content="extract is not available: no LLM bound", is_error=True)
    text = await run_extract(
        llm=ctx.llm,
        page=ctx.controller.page,
        instruction=instruction,
        extractor_system=ctx.extractor_system,
    )
    return ToolResult(content=text)


async def _remember(ctx: ToolContext, key: str, value: str) -> ToolResult:
    ctx.context.remember(key, value)
    return ToolResult(content=f"remembered {key!r}")


async def _recall(ctx: ToolContext, key: str) -> ToolResult:
    val = ctx.context.recall(key)
    if val is None:
        return ToolResult(content=f"no scratchpad entry under {key!r}", is_error=True)
    return ToolResult(content=val)


async def _ask_user(ctx: ToolContext, question: str) -> ToolResult:
    if ctx.channel is None:
        return ToolResult(
            content="ask_user is not available: no IO channel bound",
            is_error=True,
        )
    answer = await ctx.channel.ask(question)
    return ToolResult(content=f"user answered: {answer}")


async def _done(ctx: ToolContext, report: str) -> ToolResult:
    return ToolResult(content="task complete", is_terminal=True, final_report=report)


# --- Tool table ------------------------------------------------------------
#
# Order here is the order the LLM sees in its tool list. Read-mostly tools
# come first; mutating ones after; bookkeeping last.

_TOOLS: list[tuple[str, str, type[BaseModel], Handler]] = [
    (
        "observe",
        "Snapshot the current page. Returns URL, title, and an accessibility-tree YAML "
        "where interactive elements are tagged [ref=eN]. Pass those refs to "
        "click/type/press_key. Refs are valid only until the next observe().",
        ObserveArgs,
        _observe,
    ),
    (
        "navigate",
        "Navigate the browser to an absolute URL (must include https://).",
        NavigateArgs,
        _navigate,
    ),
    (
        "click",
        "Click an element by its ref from the latest observe() — pass the string "
        "after `ref=`, e.g. `e6`. If the ref is stale or unknown the result is an "
        "error and you must call observe() again first.",
        ClickArgs,
        _click,
    ),
    (
        "type",
        "Type text into a textbox/searchbox/contenteditable by ref. Set `submit: true` "
        "to press Enter after typing (useful for search boxes). The field is cleared first.",
        TypeArgs,
        _type,
    ),
    (
        "press_key",
        "Press a key while focusing the given element. Useful for Tab, Escape, "
        "ArrowDown, Enter on inputs that ignore submit. Key syntax follows "
        "Playwright (e.g. `Enter`, `Escape`, `Control+A`).",
        PressKeyArgs,
        _press_key,
    ),
    (
        "select",
        "Choose a value in a native <select> dropdown. For custom dropdowns built "
        "from divs, use click instead.",
        SelectArgs,
        _select,
    ),
    (
        "scroll",
        "Scroll the page. `direction` is one of: down, up, top, bottom. `amount` is "
        "in pixels for up/down (default 800). Use top/bottom to jump.",
        ScrollArgs,
        _scroll,
    ),
    (
        "wait_for",
        "Wait for a page condition. `condition` is one of: network_idle, load, "
        "url_contains, text_visible. url_contains and text_visible require `value`. "
        "Errors on timeout.",
        WaitForArgs,
        _wait_for,
    ),
    (
        "go_back",
        "Browser history: go back one page.",
        GoBackArgs,
        _go_back,
    ),
    (
        "go_forward",
        "Browser history: go forward one page.",
        GoForwardArgs,
        _go_forward,
    ),
    (
        "extract",
        "Extract structured information from the current page. `instruction` describes "
        "what you want. Use this for reading lists (emails, search results, prices), "
        "summarising long articles, or pulling specific values from cluttered pages. "
        "Cheaper and more reliable than parsing observe() output yourself.",
        ExtractArgs,
        _extract,
    ),
    (
        "remember",
        "Save a piece of information to your scratchpad under a key. The scratchpad is "
        "appended to your system prompt every step, so use it for anything you need to "
        "recall later: lists of candidate items, the user's resume bullet points, ids "
        "you've already processed, etc. Older observe() snapshots get compressed; the "
        "scratchpad does not.",
        RememberArgs,
        _remember,
    ),
    (
        "recall",
        "Read a value back from your scratchpad. Returns an error if no entry under that "
        "key. (You can also see all current entries in the system prompt under '# Scratchpad'.)",
        RecallArgs,
        _recall,
    ),
    (
        "ask_user",
        "Ask the user a question and wait for their reply. Use this only when you "
        "genuinely need information you cannot get from the browser (preference, missing "
        "detail, confirmation before an irreversible action). Do not use it for routine "
        "status updates.",
        AskUserArgs,
        _ask_user,
    ),
    (
        "done",
        "Finish the task. `report` is shown to the user as the final answer. Call this "
        "when the task is complete, when blocked (explain why in `report`), or when you "
        "have reached a checkpoint that requires the user's confirmation (e.g. before "
        "payment).",
        DoneArgs,
        _done,
    ),
]


def build_registry() -> ToolRegistry:
    reg = ToolRegistry()
    for name, description, arg_model, handler in _TOOLS:
        reg.register(name=name, description=description, arg_model=arg_model, handler=handler)
    return reg
