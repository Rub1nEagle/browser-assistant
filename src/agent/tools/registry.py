from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from ..browser.controller import BrowserController, BrowserError, StaleElementError
from ..context.manager import ContextManager
from ..io.channel import IOChannel
from ..llm.base import LLMClient
from ..llm.types import Tool
from .extract import run_extract


@dataclass
class ToolContext:
    controller: BrowserController
    context: ContextManager
    llm: Optional[LLMClient] = None
    channel: Optional[IOChannel] = None
    extractor_system: str = ""


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


@dataclass
class ToolRegistry:
    specs: dict[str, ToolSpec] = field(default_factory=dict)

    def register(self, tool: Tool, handler: Handler) -> None:
        self.specs[tool.name] = ToolSpec(tool=tool, handler=handler)

    def tools(self) -> list[Tool]:
        return [s.tool for s in self.specs.values()]

    async def dispatch(self, name: str, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        spec = self.specs.get(name)
        if spec is None:
            return ToolResult(content=f"unknown tool: {name}", is_error=True)
        try:
            return await spec.handler(ctx, **args)
        except StaleElementError as e:
            return ToolResult(content=str(e), is_error=True)
        except BrowserError as e:
            return ToolResult(content=f"browser error: {e}", is_error=True)
        except TypeError as e:
            return ToolResult(content=f"bad arguments to {name}: {e}", is_error=True)


# --- Tool handlers ---------------------------------------------------------


async def _navigate(ctx: ToolContext, url: str) -> ToolResult:
    return ToolResult(content=await ctx.controller.navigate(url))


async def _observe(ctx: ToolContext) -> ToolResult:
    result = await ctx.controller.observe()
    return ToolResult(content=result.rendered)


async def _click(ctx: ToolContext, element_id: str) -> ToolResult:
    return ToolResult(content=await ctx.controller.click(str(element_id)))


async def _type(ctx: ToolContext, element_id: str, text: str, submit: bool = False) -> ToolResult:
    return ToolResult(content=await ctx.controller.type_text(
        str(element_id), str(text), submit=bool(submit),
    ))


async def _press_key(ctx: ToolContext, element_id: str, key: str) -> ToolResult:
    return ToolResult(content=await ctx.controller.press_key(str(element_id), str(key)))


async def _select(ctx: ToolContext, element_id: str, value: str) -> ToolResult:
    return ToolResult(content=await ctx.controller.select_option(str(element_id), str(value)))


async def _scroll(ctx: ToolContext, direction: str, amount: int = 800) -> ToolResult:
    return ToolResult(content=await ctx.controller.scroll(direction, amount=int(amount)))


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
        instruction=str(instruction),
        extractor_system=ctx.extractor_system,
    )
    return ToolResult(content=text)


async def _remember(ctx: ToolContext, key: str, value: str) -> ToolResult:
    ctx.context.remember(str(key), str(value))
    return ToolResult(content=f"remembered {key!r}")


async def _recall(ctx: ToolContext, key: str) -> ToolResult:
    val = ctx.context.recall(str(key))
    if val is None:
        return ToolResult(content=f"no scratchpad entry under {key!r}", is_error=True)
    return ToolResult(content=val)


async def _ask_user(ctx: ToolContext, question: str) -> ToolResult:
    if ctx.channel is None:
        return ToolResult(
            content="ask_user is not available: no IO channel bound",
            is_error=True,
        )
    answer = await ctx.channel.ask(str(question))
    return ToolResult(content=f"user answered: {answer}")


async def _done(ctx: ToolContext, report: str) -> ToolResult:
    return ToolResult(content="task complete", is_terminal=True, final_report=str(report))


# --- Tool definitions ------------------------------------------------------


def _build_tool_defs() -> list[tuple[Tool, Handler]]:
    """Centralised list of (Tool, handler) pairs. Order here is the order
    they appear in the LLM's tool list — keep frequently-used ones first."""
    return [
        (
            Tool(
                name="observe",
                description=(
                    "Snapshot the current page. Returns URL, title, and an "
                    "accessibility-tree YAML where interactive elements are "
                    "tagged [ref=eN]. Pass those refs to click/type/press_key. "
                    "Refs are valid only until the next observe()."
                ),
                input_schema={"type": "object", "properties": {}},
            ),
            _observe,
        ),
        (
            Tool(
                name="navigate",
                description="Navigate the browser to an absolute URL (must include https://).",
                input_schema={
                    "type": "object",
                    "properties": {"url": {"type": "string"}},
                    "required": ["url"],
                },
            ),
            _navigate,
        ),
        (
            Tool(
                name="click",
                description=(
                    "Click an element by its ref from the latest observe() — "
                    "pass the string after `ref=`, e.g. `e6`. If the ref is "
                    "stale or unknown the result is an error and you must call "
                    "observe() again first."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "element_id": {
                            "type": "string",
                            "description": "Ref like `e6` from the latest observe().",
                        },
                    },
                    "required": ["element_id"],
                },
            ),
            _click,
        ),
        (
            Tool(
                name="type",
                description=(
                    "Type text into a textbox/searchbox/contenteditable by ref. "
                    "Set `submit: true` to press Enter after typing (useful for "
                    "search boxes). The field is cleared first."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "element_id": {"type": "string"},
                        "text": {"type": "string"},
                        "submit": {"type": "boolean", "default": False},
                    },
                    "required": ["element_id", "text"],
                },
            ),
            _type,
        ),
        (
            Tool(
                name="press_key",
                description=(
                    "Press a key while focusing the given element. Useful for "
                    "Tab, Escape, ArrowDown, Enter on inputs that ignore submit. "
                    "Key syntax follows Playwright (e.g. `Enter`, `Escape`, `Control+A`)."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "element_id": {"type": "string"},
                        "key": {"type": "string"},
                    },
                    "required": ["element_id", "key"],
                },
            ),
            _press_key,
        ),
        (
            Tool(
                name="select",
                description=(
                    "Choose a value in a native <select> dropdown. For custom "
                    "dropdowns built from divs, use click instead."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "element_id": {"type": "string"},
                        "value": {"type": "string"},
                    },
                    "required": ["element_id", "value"],
                },
            ),
            _select,
        ),
        (
            Tool(
                name="scroll",
                description=(
                    "Scroll the page. `direction` is one of: down, up, top, bottom. "
                    "`amount` is in pixels for up/down (default 800). Use top/bottom "
                    "to jump."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "direction": {"type": "string", "enum": ["down", "up", "top", "bottom"]},
                        "amount": {"type": "integer", "default": 800},
                    },
                    "required": ["direction"],
                },
            ),
            _scroll,
        ),
        (
            Tool(
                name="wait_for",
                description=(
                    "Wait for a page condition. `condition` is one of: "
                    "network_idle, load, url_contains, text_visible. "
                    "url_contains and text_visible require `value`. "
                    "Errors on timeout."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "condition": {
                            "type": "string",
                            "enum": ["network_idle", "load", "url_contains", "text_visible"],
                        },
                        "value": {"type": "string"},
                        "timeout_seconds": {"type": "number", "default": 10},
                    },
                    "required": ["condition"],
                },
            ),
            _wait_for,
        ),
        (
            Tool(
                name="go_back",
                description="Browser history: go back one page.",
                input_schema={"type": "object", "properties": {}},
            ),
            _go_back,
        ),
        (
            Tool(
                name="go_forward",
                description="Browser history: go forward one page.",
                input_schema={"type": "object", "properties": {}},
            ),
            _go_forward,
        ),
        (
            Tool(
                name="extract",
                description=(
                    "Extract structured information from the current page. "
                    "`instruction` describes what you want. Use this for reading "
                    "lists (emails, search results, prices), summarising long "
                    "articles, or pulling specific values from cluttered pages. "
                    "Cheaper and more reliable than parsing observe() output yourself."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "instruction": {
                            "type": "string",
                            "description": (
                                "What to extract. E.g. 'list the top 10 emails: "
                                "subject, sender, snippet, is_unread (boolean)'."
                            ),
                        },
                    },
                    "required": ["instruction"],
                },
            ),
            _extract,
        ),
        (
            Tool(
                name="remember",
                description=(
                    "Save a piece of information to your scratchpad under a key. "
                    "The scratchpad is appended to your system prompt every step, "
                    "so use it for anything you need to recall later: lists of "
                    "candidate items, the user's resume bullet points, ids you've "
                    "already processed, etc. Older observe() snapshots get "
                    "compressed; the scratchpad does not."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "key": {"type": "string"},
                        "value": {"type": "string"},
                    },
                    "required": ["key", "value"],
                },
            ),
            _remember,
        ),
        (
            Tool(
                name="recall",
                description=(
                    "Read a value back from your scratchpad. Returns an error if "
                    "no entry under that key. (You can also see all current entries "
                    "in the system prompt under '# Scratchpad'.)"
                ),
                input_schema={
                    "type": "object",
                    "properties": {"key": {"type": "string"}},
                    "required": ["key"],
                },
            ),
            _recall,
        ),
        (
            Tool(
                name="ask_user",
                description=(
                    "Ask the user a question and wait for their reply. Use this only "
                    "when you genuinely need information you cannot get from the "
                    "browser (preference, missing detail, confirmation before an "
                    "irreversible action). Do not use it for routine status updates."
                ),
                input_schema={
                    "type": "object",
                    "properties": {"question": {"type": "string"}},
                    "required": ["question"],
                },
            ),
            _ask_user,
        ),
        (
            Tool(
                name="done",
                description=(
                    "Finish the task. `report` is shown to the user as the final "
                    "answer. Call this when the task is complete, when blocked "
                    "(explain why in `report`), or when you have reached a "
                    "checkpoint that requires the user's confirmation (e.g. before "
                    "payment)."
                ),
                input_schema={
                    "type": "object",
                    "properties": {"report": {"type": "string"}},
                    "required": ["report"],
                },
            ),
            _done,
        ),
    ]


def build_registry() -> ToolRegistry:
    reg = ToolRegistry()
    for tool, handler in _build_tool_defs():
        reg.register(tool, handler)
    return reg
