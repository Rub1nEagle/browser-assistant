from __future__ import annotations

import asyncio
import os
from pathlib import Path

import click
from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel

from agent.browser.controller import BrowserController


def _profile_dir(override: Path | None) -> Path:
    if override is not None:
        return override.resolve()
    load_dotenv()
    return Path(os.getenv("BROWSER_PROFILE_DIR", "./.browser-profile")).resolve()


def _running_in_docker() -> bool:
    # The compose file sets DISPLAY=:99 inside the container; that's the
    # most reliable signal we have without poking at /.dockerenv.
    return os.getenv("DISPLAY") == ":99" or Path("/.dockerenv").exists()


async def _run_login(url: str, profile_dir: Path) -> int:
    console = Console()
    profile_dir.mkdir(parents=True, exist_ok=True)

    controller = BrowserController(profile_dir=profile_dir)
    await controller.start()
    try:
        msg = await controller.navigate(url)
        console.print(f"[dim]{msg}[/dim]")

        if _running_in_docker():
            instructions = (
                f"Browser is open at [bold]{url}[/bold].\n\n"
                "Open [link=http://localhost:6080]http://localhost:6080[/link] in your "
                "host browser to see the noVNC view of this session, log in, then come "
                "back here and press Enter.\n\n"
                f"Profile is being saved to [dim]{profile_dir}[/dim] (volume-backed in Docker)."
            )
        else:
            instructions = (
                f"Browser window should now be open at [bold]{url}[/bold].\n\n"
                "Log in there, then come back to this terminal and press Enter.\n\n"
                f"Profile will be saved to [dim]{profile_dir}[/dim]."
            )
        console.print(Panel(instructions, title="login wizard", border_style="cyan"))

        try:
            await asyncio.to_thread(input, "[press Enter when done] ")
        except (KeyboardInterrupt, EOFError):
            console.print("[yellow]aborted; closing browser…[/yellow]")
    finally:
        await controller.stop()
    console.print("[green]done. Profile saved.[/green]")
    return 0


@click.command()
@click.argument("url")
@click.option(
    "--profile-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Override BROWSER_PROFILE_DIR.",
)
def login(url: str, profile_dir: Path | None) -> None:
    """Open URL in the browser and wait while you log in.

    Run this once per site. Cookies and session storage land in the
    persistent profile and survive `docker compose down`.
    """
    rc = asyncio.run(_run_login(url, _profile_dir(profile_dir)))
    raise SystemExit(rc)
