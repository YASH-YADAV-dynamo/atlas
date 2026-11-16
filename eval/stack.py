"""Local stack for a benchmark run: a private TestMart, sign-in, and Route API variants.

Nothing here runs on the hosted demo; the run is local and every run uses its own TestMart database.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

from atlas.adapters.hydradb.fake import InMemoryGraphStore
from atlas.contracts.api_models import (
    LocateRequest,
    LocateResponse,
    PlanRequest,
    PlanResponse,
    ReportRequest,
    ReportResponse,
)
from atlas.contracts.ports import BrowserDriver, RouteApi
from atlas.contracts.types import Action, ActionKind, Fill, Locator
from atlas.core.learner import report as learn
from atlas.core.locate import locate
from atlas.core.planner import plan
from atlas.settings import Settings

NAMESPACE = "bench"


class UtcClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class InProcessRoutes:
    """The Route API served in this process by core on an in-memory map (no HydraDB needed)."""

    def __init__(self) -> None:
        self._store, self._clock = InMemoryGraphStore(), UtcClock()

    def locate(self, request: LocateRequest) -> LocateResponse:
        return LocateResponse(**locate(self._store, NAMESPACE, request.site, request.snapshot).model_dump())

    def plan(self, request: PlanRequest) -> PlanResponse:
        return PlanResponse(**plan(self._store, NAMESPACE, request).model_dump())

    def report(self, request: ReportRequest) -> ReportResponse:
        result = learn(self._store, self._clock, NAMESPACE, request)
        return ReportResponse(state_id=result.state_id, known=result.known, duplicate=result.duplicate)


class FrozenRoutes:
    """Learning off (held-out evaluation): reports are not stored; the state is only located."""

    def __init__(self, inner: RouteApi) -> None:
        self._inner = inner

    def locate(self, request: LocateRequest) -> LocateResponse:
        return self._inner.locate(request)

    def plan(self, request: PlanRequest) -> PlanResponse:
        return self._inner.plan(request)

    def report(self, request: ReportRequest) -> ReportResponse:
        state = self._inner.locate(LocateRequest(site=request.site, snapshot=request.to_snapshot))
        return ReportResponse(state_id=state.state_id, known=state.known, duplicate=False)


class TestMart:
    """A TestMart subprocess on its own port and database file."""

    __test__ = False

    def __init__(self, workdir: Path, port: int, settings: Settings) -> None:
        self.url = f"http://127.0.0.1:{port}"
        self.db_path = str(workdir / "testmart.db")
        self._env = {
            **os.environ,
            "TESTMART_DB": self.db_path,
            "TESTMART_PORT": str(port),
            "TESTMART_HOST": "127.0.0.1",
            "TESTMART_PASSWORD": settings.testmart_password.get_secret_value(),
        }
        self._process: subprocess.Popen[bytes] | None = None

    def __enter__(self) -> TestMart:
        self._process = subprocess.Popen([sys.executable, "-m", "testmart"], env=self._env)
        for _ in range(60):
            try:
                if httpx.get(f"{self.url}/healthz", timeout=1).status_code == 200:
                    return self
            except httpx.HTTPError:
                time.sleep(0.5)
        raise RuntimeError("TestMart did not start")

    def __exit__(self, *_: object) -> None:
        if self._process is not None:
            self._process.terminate()
            self._process.wait(timeout=10)

    def reset(self, seed: int) -> None:
        httpx.post(f"{self.url}/__reset", params={"seed": seed}, timeout=30).raise_for_status()


def sign_in(driver: BrowserDriver, settings: Settings) -> None:
    driver.act(Action(kind=ActionKind.GOTO, value="/login"))
    driver.act(
        Action(
            kind=ActionKind.CLICK,
            locator=Locator(role="button", name="Sign in"),
            fills=(
                Fill(locator=Locator(role="textbox", name="Username"), value=settings.testmart_user),
                Fill(
                    locator=Locator(role="textbox", name="Password"),
                    value=settings.testmart_password.get_secret_value(),
                ),
            ),
        )
    )
