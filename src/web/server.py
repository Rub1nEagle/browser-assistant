from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from dataclasses import asdict, is_dataclass
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from agent.config import Settings
from agent.core import build_agent
from agent.events import (
    AgentStarted,
    AgentThinking,
    Event,
    EventBus,
    LLMRequestCompleted,
    LLMRequestStarted,
    NeedsUserInput,
    ScratchpadUpdated,
    TaskCompleted,
    TaskFailed,
    ToolCallCompleted,
    ToolCallStarted,
)
from agent.io.web_channel import WebChannel


PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"
STATIC_DIR = Path(__file__).resolve().parent / "static"


def _serialize(event: Event) -> dict:
    payload = {"type": type(event).__name__}
    if is_dataclass(event):
        payload.update(asdict(event))
    return payload


class AppState:
    """One-running-task-at-a-time singleton. Local tool, single user.
    A new run can't start while one is already in flight."""

    def __init__(self) -> None:
        self.bus = EventBus()
        self.web_channel = WebChannel(self.bus)
        self._queues: set[asyncio.Queue] = set()
        self._current: asyncio.Task | None = None
        # Cumulative cost of the current run, exposed to the UI for the
        # cost meter (LLMRequestCompleted carries per-step cost too, but
        # the UI prefers a single running total).
        self._cumulative_cost = 0.0
        # True when at least one step had unknown pricing — UI appends "+?".
        self._cost_partial = False
        self.bus.subscribe(self._on_event)

    # --- WS plumbing -----------------------------------------------------

    def attach(self, q: asyncio.Queue) -> None:
        self._queues.add(q)

    def detach(self, q: asyncio.Queue) -> None:
        self._queues.discard(q)

    async def _on_event(self, event: Event) -> None:
        if isinstance(event, LLMRequestCompleted):
            if event.cost_usd is None:
                self._cost_partial = True
            else:
                self._cumulative_cost += event.cost_usd
        payload = _serialize(event)
        if isinstance(event, LLMRequestCompleted):
            payload["cumulative_cost_usd"] = self._cumulative_cost
            payload["cost_partial"] = self._cost_partial
        for q in list(self._queues):
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                # Drop on slow clients; the UI is best-effort.
                pass

    # --- run lifecycle --------------------------------------------------

    def is_running(self) -> bool:
        return self._current is not None and not self._current.done()

    async def start_run(self, task: str) -> None:
        if self.is_running():
            raise RuntimeError("a task is already running")
        self._cumulative_cost = 0.0
        self._cost_partial = False
        self._current = asyncio.create_task(self._run(task))

    async def _run(self, task: str) -> None:
        settings = Settings.load()
        system_prompt = (PROMPTS_DIR / "system.md").read_text(encoding="utf-8")
        extractor_prompt = (PROMPTS_DIR / "extractor.md").read_text(encoding="utf-8")
        agent, controller = await build_agent(
            settings=settings,
            bus=self.bus,
            system_prompt=system_prompt,
            extractor_prompt=extractor_prompt,
            channel=self.web_channel,
        )
        await controller.start()
        try:
            await agent.run(task)
        except Exception as e:  # noqa: BLE001 — surface any crash to the UI
            await self.bus.emit(TaskFailed(reason=f"unexpected error: {e!r}"))
        finally:
            await controller.stop()
            self.web_channel.cancel("run ended")

    async def cancel_run(self) -> None:
        if self.is_running():
            assert self._current is not None
            self._current.cancel()
            try:
                await self._current
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self.web_channel.cancel("cancelled by user")


# --- FastAPI app -----------------------------------------------------------


@asynccontextmanager
async def _lifespan(app: FastAPI):
    yield
    state: AppState = app.state.app_state
    await state.cancel_run()


def create_app() -> FastAPI:
    app = FastAPI(title="browser-assistant", lifespan=_lifespan)
    app.state.app_state = AppState()

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/")
    async def root() -> FileResponse:
        return FileResponse(str(STATIC_DIR / "index.html"))

    @app.get("/health")
    async def health() -> dict:
        return {
            "ok": True,
            "running": app.state.app_state.is_running(),
            "novnc_url": os.getenv("NOVNC_URL", "http://localhost:6080/vnc.html?autoconnect=1&resize=remote"),
        }

    @app.websocket("/ws")
    async def ws_endpoint(websocket: WebSocket) -> None:
        await websocket.accept()
        state: AppState = app.state.app_state
        q: asyncio.Queue = asyncio.Queue(maxsize=1024)
        state.attach(q)

        async def sender() -> None:
            try:
                while True:
                    payload = await q.get()
                    await websocket.send_json(payload)
            except (WebSocketDisconnect, RuntimeError):
                pass

        send_task = asyncio.create_task(sender())
        try:
            while True:
                msg = await websocket.receive_json()
                kind = msg.get("type")
                if kind == "run":
                    task = (msg.get("task") or "").strip()
                    if not task:
                        await websocket.send_json({"type": "Error", "reason": "empty task"})
                        continue
                    if state.is_running():
                        await websocket.send_json({"type": "Error", "reason": "another task is running"})
                        continue
                    try:
                        await state.start_run(task)
                    except Exception as e:  # noqa: BLE001
                        await websocket.send_json({"type": "Error", "reason": str(e)})
                elif kind == "answer":
                    answer = msg.get("answer", "")
                    delivered = state.web_channel.deliver_answer(answer)
                    if not delivered:
                        await websocket.send_json({
                            "type": "Error",
                            "reason": "no pending question",
                        })
                elif kind == "cancel":
                    await state.cancel_run()
                elif kind == "ping":
                    await websocket.send_json({"type": "pong"})
                else:
                    await websocket.send_json({
                        "type": "Error",
                        "reason": f"unknown message type: {kind!r}",
                    })
        except WebSocketDisconnect:
            pass
        finally:
            send_task.cancel()
            state.detach(q)

    return app


app = create_app()
