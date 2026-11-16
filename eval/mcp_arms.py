"""Arms M0 and M1: a tool-calling agent with browser tools only (M0), and the same plus the Atlas MCP
server in runner mode over stdio (M1). Both run `run_tool_loop`; the record shows round trips and
context tokens next to the provider's token usage."""

from __future__ import annotations

import asyncio
import sys
import threading
import time
from collections.abc import Coroutine
from concurrent.futures import Future
from types import TracebackType
from typing import Any, Self

from mcp import Client
from mcp.client.stdio import StdioServerParameters
from pydantic import JsonValue

from atlas.contracts.bench_models import Arm, BenchTask, LoopStats, TaskRecord, ToolDef, ToolModel
from atlas.contracts.errors import BrowserActionError
from atlas.contracts.ports import BrowserDriver
from atlas.contracts.types import Action, ActionKind, Locator
from eval.arms import ArmContext
from eval.mcp_loop import ToolFn, run_tool_loop

type Tools = dict[str, tuple[ToolDef, ToolFn]]

MAX_PAGE_ELEMENTS = 60


def _schema(**props: dict[str, JsonValue]) -> dict[str, JsonValue]:
    required: list[JsonValue] = [k for k, v in props.items() if v.pop("required", False)]
    return {"type": "object", "properties": dict(props), "required": required}


def _text(description: str, required: bool = False) -> dict[str, JsonValue]:
    return {"type": "string", "description": description, "required": required}


def browser_tools(driver: BrowserDriver) -> Tools:
    """goto / snapshot / click / fill on a BrowserDriver; one page snapshot per step."""

    def run(action: Action) -> str:
        try:
            driver.act(action)
        except BrowserActionError as error:
            return f"Error: {error}"
        return "ok"

    def goto(args: dict[str, JsonValue]) -> str:
        return run(Action(kind=ActionKind.GOTO, value=str(args.get("path", "/"))))

    def snapshot(args: dict[str, JsonValue]) -> str:
        page = driver.snapshot()
        lines = [f"{e.role} | {e.name}{' (list)' if e.in_list else ''}" for e in page.elements]
        return f"{page.title} ({page.url})\n" + "\n".join(lines[:MAX_PAGE_ELEMENTS]) + f"\n\n{page.text}"

    def click(args: dict[str, JsonValue]) -> str:
        row = args.get("row")
        locator = Locator(
            role=str(args.get("role", "link")), name=str(args.get("name", "")), row=str(row) if row else None
        )
        return run(Action(kind=ActionKind.CLICK, locator=locator))

    def fill(args: dict[str, JsonValue]) -> str:
        locator = Locator(role="textbox", name=str(args.get("name", "")))
        return run(Action(kind=ActionKind.FILL, locator=locator, value=str(args.get("value", ""))))

    definitions: list[tuple[ToolDef, ToolFn]] = [
        (
            ToolDef(
                name="browser_goto",
                description="Open a path on the site, for example /orders.",
                input_schema=_schema(path=_text("Path starting with /", True)),
            ),
            goto,
        ),
        (
            ToolDef(
                name="browser_snapshot",
                description="List the page's interactive controls (role | name) and its text.",
                input_schema=_schema(),
            ),
            snapshot,
        ),
        (
            ToolDef(
                name="browser_click",
                description="Click a control by role and exact name; row picks the list row with that text.",
                input_schema=_schema(
                    role=_text("link, button, checkbox ...", True),
                    name=_text("Exact control name", True),
                    row=_text("Row text"),
                ),
            ),
            click,
        ),
        (
            ToolDef(
                name="browser_fill",
                description="Type a value into a text box by its exact name.",
                input_schema=_schema(
                    name=_text("Exact control name", True), value=_text("Text to type", True)
                ),
            ),
            fill,
        ),
    ]
    return {d.name: (d, fn) for d, fn in definitions}


class McpBridge:
    """A synchronous view of one MCP stdio server: tools as `(ToolDef, callable)` pairs."""

    def __init__(self, params: StdioServerParameters) -> None:
        self._params = params
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._client: Client | None = None
        self._opened = threading.Event()
        self._stop = asyncio.Event()
        self._session_task: Future[None] | None = None

    def _run[T](self, coro: Coroutine[Any, Any, T]) -> T:
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result()

    async def _session(self) -> None:
        """Open and close the client in one task (the stdio transport requires it)."""
        async with Client(self._params) as client:
            self._client = client
            self._opened.set()
            await self._stop.wait()

    def __enter__(self) -> Self:
        self._thread.start()
        task = asyncio.run_coroutine_threadsafe(self._session(), self._loop)
        self._session_task = task
        task.add_done_callback(lambda _: self._opened.set())  # a failed start must not hang
        self._opened.wait(timeout=60)
        if self._client is None:
            task.result(timeout=1)  # raises the start-up error
            raise RuntimeError("the Atlas MCP server did not start")
        return self

    def __exit__(
        self, et: type[BaseException] | None, e: BaseException | None, tb: TracebackType | None
    ) -> None:
        self._loop.call_soon_threadsafe(self._stop.set)
        if self._session_task is not None:
            self._session_task.result(timeout=30)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)

    def tools(self) -> Tools:
        client = self._client
        assert client is not None

        def caller(name: str) -> ToolFn:
            def call(args: dict[str, JsonValue]) -> str:
                result = self._run(client.call_tool(name, dict(args)))
                return "".join(getattr(part, "text", "") for part in result.content)

            return call

        listing = self._run(client.list_tools())
        return {
            t.name: (
                ToolDef(
                    name=t.name, description=(t.description or "")[:400], input_schema=dict(t.input_schema)
                ),
                caller(t.name),
            )
            for t in listing.tools
        }


def run_m(arm: Arm, task: BenchTask, ctx: ArmContext, model: ToolModel, tools: Tools) -> TaskRecord:
    started = time.monotonic()
    stats: LoopStats = run_tool_loop(model, tools, start_path_note(task), max_turns=ctx.max_steps)
    return TaskRecord(
        run_id=ctx.run_id,
        arm=arm,
        seed=ctx.seed,
        iteration=ctx.iteration,
        split=ctx.split,
        template_id=task.template_id,
        success=ctx.check(stats.answer),
        model_calls=stats.model_calls,
        input_tokens=stats.input_tokens,
        output_tokens=stats.output_tokens,
        tool_calls=stats.tool_calls,
        context_tokens=stats.context_tokens,
        actions=stats.tool_calls,
        wall_ms=max(0, int((time.monotonic() - started) * 1000)),
    )


def atlas_server_params(env: dict[str, str]) -> StdioServerParameters:
    return StdioServerParameters(command=sys.executable, args=["-m", "atlas.mcp"], env=env)


def start_path_note(task: BenchTask) -> str:
    """The task text M arms see: goal plus the page they start on (they navigate themselves)."""
    return f"{task.spec.goal_text} (the browser is on {task.spec.start_path})"
