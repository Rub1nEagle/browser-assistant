from __future__ import annotations

import asyncio
import json
from pathlib import Path

import click
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from agent.config import Settings
from agent.core import build_agent
from cli.login import login as _login_command
from agent.events import (
    AgentStarted,
    AgentThinking,
    Event,
    EventBus,
    LLMRequestCompleted,
    LLMRequestStarted,
    TaskCompleted,
    TaskFailed,
    ToolCallCompleted,
    ToolCallStarted,
)


PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"


def _format_args(args: dict) -> str:
    if not args:
        return ""
    try:
        return json.dumps(args, ensure_ascii=False, separators=(", ", "="))
    except (TypeError, ValueError):
        return repr(args)


def _make_console_listener(console: Console):
    state = {"step": 0, "cumulative_cost": 0.0}

    def on_event(event: Event) -> None:
        if isinstance(event, AgentStarted):
            console.print(Panel(event.task, title="task", border_style="cyan"))
        elif isinstance(event, LLMRequestStarted):
            state["step"] = event.step
            console.print(f"[dim][step {event.step}] thinking…[/dim]")
        elif isinstance(event, LLMRequestCompleted):
            state["cumulative_cost"] += event.cost_usd
            console.print(
                f"[dim][step {event.step}] in={event.input_tokens} "
                f"out={event.output_tokens} cache_r={event.cache_read_tokens} "
                f"cache_w={event.cache_creation_tokens} "
                f"step=${event.cost_usd:.4f} total=${state['cumulative_cost']:.4f}[/dim]"
            )
        elif isinstance(event, AgentThinking):
            console.print(Text(event.text, style="italic dim"))
        elif isinstance(event, ToolCallStarted):
            args_str = _format_args(event.args)
            label = f"{event.tool}({args_str})" if args_str else f"{event.tool}()"
            console.print(f"[yellow]→[/yellow] [bold]{label}[/bold]")
        elif isinstance(event, ToolCallCompleted):
            mark = "[red]✗[/red]" if event.is_error else "[green]✓[/green]"
            style = "red" if event.is_error else "green"
            console.print(f"  {mark} [{style}]{event.result_summary}[/{style}]")
        elif isinstance(event, TaskCompleted):
            console.print(Panel(event.report, title="done", border_style="green"))
        elif isinstance(event, TaskFailed):
            console.print(Panel(event.reason, title="failed", border_style="red"))

    return on_event


async def _run(task: str, *, profile_dir: Path | None) -> int:
    console = Console()
    settings = Settings.load()
    bus = EventBus()
    bus.subscribe(_make_console_listener(console))

    system_prompt = (PROMPTS_DIR / "system.md").read_text(encoding="utf-8")
    extractor_prompt = (PROMPTS_DIR / "extractor.md").read_text(encoding="utf-8")
    agent, controller = await build_agent(
        settings=settings,
        bus=bus,
        system_prompt=system_prompt,
        extractor_prompt=extractor_prompt,
        profile_dir=profile_dir,
    )
    await controller.start()
    try:
        result = await agent.run(task)
    finally:
        await controller.stop()
    return 0 if result.final_report is not None else 1


@click.group()
def cli() -> None:
    """Browser-assistant CLI."""


@cli.command()
@click.argument("task", nargs=-1, required=True)
@click.option(
    "--profile-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Override BROWSER_PROFILE_DIR.",
)
def run(task: tuple[str, ...], profile_dir: Path | None) -> None:
    """Run the agent on a single TASK string."""
    task_str = " ".join(task)
    rc = asyncio.run(_run(task_str, profile_dir=profile_dir))
    raise SystemExit(rc)


@cli.command()
@click.option("--host", default="0.0.0.0", show_default=True)
@click.option("--port", default=8000, show_default=True, type=int)
def serve(host: str, port: int) -> None:
    """Start the web UI (FastAPI + WebSocket) on HOST:PORT."""
    import uvicorn
    uvicorn.run("web.server:app", host=host, port=port, log_level="info")


cli.add_command(_login_command)


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
