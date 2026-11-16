"""Demo UI router: presets, run-as-SSE with a small concurrency cap and a wait queue."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections import defaultdict, deque
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from typing import cast

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from atlas.contracts.api_models import ChangesResponse
from atlas.contracts.errors import BudgetExceededError
from atlas.contracts.ports import Clock, GraphStore, RouteStore
from atlas.contracts.types import (
    Frozen,
    GoalText,
    MapView,
    Params,
    RunResult,
    StepLog,
    TaskSpec,
)
from atlas.settings import Settings

RunFn = Callable[[TaskSpec, Callable[[StepLog], None], int], RunResult]  # third: model-call allowance

WINDOW = timedelta(hours=1)

_PRESET_SEED = 7
_PRESET_COUNT = 6

Payload = BaseModel | dict[str, object]


class DemoRunRequest(Frozen):
    goal_text: GoalText
    params: Params = Field(default_factory=dict)


class _Limiter:
    """At most `max_running` active tickets; up to `max_queued` more wait in FIFO order."""

    def __init__(self, max_running: int, max_queued: int) -> None:
        self._max_running = max(1, max_running)
        self._max_queued = max(0, max_queued)
        self._running = 0
        self._waiting: deque[asyncio.Future[None]] = deque()

    def admit(self) -> asyncio.Future[None]:
        """Return a future that resolves when a slot is held; raise 429 when the queue is full."""
        fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        if self._running < self._max_running and not self._waiting:
            self._running += 1
            fut.set_result(None)
        elif len(self._waiting) >= self._max_queued:
            raise HTTPException(status_code=429, detail="The demo is busy; try again shortly.")
        else:
            self._waiting.append(fut)
        return fut

    def position(self, fut: asyncio.Future[None]) -> int:
        try:
            return list(self._waiting).index(fut) + 1
        except ValueError:
            return 0

    def abandon(self, fut: asyncio.Future[None]) -> None:
        """A waiter gave up: leave the queue, or return the slot if it was just granted."""
        if fut in self._waiting:
            self._waiting.remove(fut)
        elif fut.done() and not fut.cancelled():
            self.release()

    def release(self) -> None:
        self._running -= 1
        while self._waiting:
            nxt = self._waiting.popleft()
            if not nxt.done():
                self._running += 1
                nxt.set_result(None)
                return


def _sse(event: str, data: Payload) -> str:
    payload = data.model_dump_json() if isinstance(data, BaseModel) else json.dumps(data)
    return f"event: {event}\ndata: {payload}\n\n"


class _UtcClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


def client_ip(request: Request) -> str:
    """The last X-Forwarded-For entry (appended by our own proxy), else the socket address."""
    forwarded = request.headers.get("x-forwarded-for", "")
    last = forwarded.rsplit(",", 1)[-1].strip()
    if last:
        return last
    return request.client.host if request.client else "unknown"


def build_demo_router(
    settings: Settings,
    run: RunFn,
    *,
    budget_status: Callable[[], str] = lambda: "ok",
    clock: Clock | None = None,
    store: GraphStore | None = None,
) -> APIRouter:
    clock = clock or _UtcClock()
    uses: defaultdict[str, list[datetime]] = defaultdict(list)  # per-IP model-using runs, pruned by time
    uses_lock = threading.Lock()

    def allowance(ip: str) -> int:
        cutoff = clock.now() - WINDOW
        with uses_lock:
            for key in [k for k, v in uses.items() if not v or v[-1] <= cutoff]:
                del uses[key]
            recent = [t for t in uses.get(ip, []) if t > cutoff]
        if budget_status() == "exhausted" or len(recent) >= settings.demo_model_runs_per_ip_hour:
            return 0
        return settings.demo_max_explore_steps

    def record_use(ip: str) -> None:
        with uses_lock:
            uses[ip].append(clock.now())

    router = APIRouter(prefix="/demo")
    limiter = _Limiter(settings.demo_max_concurrent_runs, settings.demo_max_queued_runs)
    site: str = settings.mcp_site  # single source of truth for the site name

    @router.get("/config")
    def config() -> dict[str, object]:
        return {
            "site": site,
            "max_concurrent": settings.demo_max_concurrent_runs,
            "max_queued": settings.demo_max_queued_runs,
            "budget": budget_status(),
            "model_runs_per_ip_hour": settings.demo_model_runs_per_ip_hour,
            "max_explore_steps": settings.demo_max_explore_steps,
        }

    @router.get("/map")
    def demo_map() -> MapView:
        """The demo site's map, read-only and unauthenticated; site and namespace are fixed here."""
        if store is None:
            raise HTTPException(status_code=404, detail="No map store configured.")
        return store.get_map(settings.atlas_namespace, site)

    @router.get("/changes")
    def demo_changes() -> ChangesResponse:
        """The demo site's stopped-working steps (read-only, no client-chosen site)."""
        if store is None:
            raise HTTPException(status_code=404, detail="No map store configured.")
        return ChangesResponse(
            site=site, changes=cast(RouteStore, store).list_changes(settings.atlas_namespace, site)
        )

    @router.get("/presets")
    def presets() -> list[dict[str, object]]:
        from testmart.tasks import generate_tasks

        return [
            {"goal_text": t.text, "params": t.params, "template_id": t.template_id}
            for t in generate_tasks(_PRESET_COUNT, _PRESET_SEED)
        ]

    @router.post("/mutate")
    def mutate(v: int = Query(2, ge=1, le=3)) -> dict[str, object]:
        """Switch TestMart's UI version (2 by default; 1 restores the original) so the demo can
        show a site change. TestMart's switch is unauthenticated, like /__reset."""
        try:
            resp = httpx.post(
                f"{settings.testmart_url.rstrip('/')}/admin/mutate", params={"v": v}, timeout=10.0
            )
            resp.raise_for_status()
        except httpx.HTTPError:
            raise HTTPException(status_code=502, detail="Could not switch the demo site.") from None
        return {"version": v}

    @router.post("/run")
    async def run_task(req: DemoRunRequest, request: Request) -> StreamingResponse:
        spec = TaskSpec(site=site, goal_text=req.goal_text, params=req.params)
        ticket = limiter.admit()  # raises 429 before any streaming starts
        return StreamingResponse(
            _stream(spec, ticket, client_ip(request)),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    async def _stream(spec: TaskSpec, ticket: asyncio.Future[None], ip: str) -> AsyncIterator[str]:
        loop = asyncio.get_running_loop()
        handed_off = False
        try:
            last_pos = -1
            while not ticket.done():
                pos = limiter.position(ticket)
                if pos != last_pos:
                    last_pos = pos
                    yield _sse("queued", {"position": pos})
                await asyncio.wait([ticket], timeout=0.5)
            yield _sse("started", {})
            events: asyncio.Queue[tuple[str, Payload]] = asyncio.Queue()

            def put(kind: str, item: Payload) -> None:
                loop.call_soon_threadsafe(events.put_nowait, (kind, item))

            def work() -> None:
                try:
                    result = run(spec, lambda s: put("step", s), allowance(ip))
                    if result.model_calls > 0:
                        record_use(ip)
                    put("result", result)
                except BudgetExceededError:
                    put("error", {"message": "The model budget is used up; only known routes run."})
                except Exception as exc:
                    logging.getLogger("atlas.demo").error(
                        "demo run failed: %s: %s", type(exc).__name__, str(exc)[:300]
                    )
                    put("error", {"message": "The run failed."})
                finally:
                    loop.call_soon_threadsafe(limiter.release)
                    put("end", {})

            handed_off = True
            threading.Thread(target=work, daemon=True).start()
            while True:
                kind, item = await events.get()
                if kind == "end":
                    return
                yield _sse(kind, item)
        finally:
            if not handed_off:
                limiter.abandon(ticket)

    return router


__all__ = ["RunFn", "build_demo_router"]
