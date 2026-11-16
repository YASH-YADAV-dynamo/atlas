"""Demo cost limits (cost guard, lane G). Targets atlas/demo/router.py:

    RunFn = Callable[[TaskSpec, Callable[[StepLog], None], int], RunResult]
        the third argument is the model-call allowance for this run
    build_demo_router(settings, run, *, budget_status: Callable[[], str] = ..., clock: Clock | None = None)

Rules: a run's allowance is DEMO_MAX_EXPLORE_STEPS, or 0 when its client IP already had
DEMO_MODEL_RUNS_PER_IP_HOUR runs that used the model within the last hour, or when the budget
is exhausted. Runs that used no model call never count. The client IP is the last entry of
X-Forwarded-For (the one our own proxy appended), else the socket address.
"""

from __future__ import annotations

import inspect
import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from atlas.contracts.errors import BudgetExceededError
from atlas.contracts.types import RunResult
from atlas.demo.router import build_demo_router
from atlas.settings import Settings

if "budget_status" not in inspect.signature(build_demo_router).parameters:
    pytest.skip("cost guard: demo limits implemented by lane G", allow_module_level=True)

KEY = "sk-test-not-a-real-key"


class Clock:
    def __init__(self) -> None:
        self.at = datetime(2026, 11, 16, 6, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self.at


class Demo:
    def __init__(self, **overrides) -> None:
        self.allowances: list[int] = []
        self.budget = "ok"
        self.clock = Clock()
        self.fail = False
        settings = Settings(_env_file=None, model_api_key=KEY, **overrides)  # type: ignore[call-arg]
        app = FastAPI()
        router = build_demo_router(settings, self._run, budget_status=lambda: self.budget, clock=self.clock)
        app.include_router(router)
        self.client = TestClient(app)

    def _run(self, spec, on_step, allowance: int) -> RunResult:
        """A stand-in for the runner: 'known ...' goals ride the map, others need the model."""
        self.allowances.append(allowance)
        if self.fail:
            raise BudgetExceededError("model budget exhausted")
        known = spec.goal_text.startswith("known")
        calls = 0 if known else min(2, allowance)
        success = known or allowance > 0
        reason = "" if success else "explore_limit"
        return RunResult(
            success=success,
            model_calls=calls,
            model_tokens=0,
            replans=0,
            wall_ms=1,
            steps=(),
            stop_reason=reason,
        )

    def run(self, goal: str, ip: str = "203.0.113.5") -> dict:
        headers = {"X-Forwarded-For": f"198.51.100.1, {ip}"}
        response = self.client.post("/demo/run", json={"goal_text": goal, "params": {}}, headers=headers)
        assert response.status_code == 200, response.text
        events = {}
        for block in response.text.strip().split("\n\n"):
            lines = dict(line.split(": ", 1) for line in block.splitlines())
            events[lines["event"]] = json.loads(lines["data"])
        return events


def test_model_using_runs_are_capped_per_ip_per_hour():
    """R-COST-3: after 3 model-using runs in an hour an IP only gets map-only runs; other IPs
    are unaffected; the cap lifts when the hour has passed."""
    demo = Demo()
    for _ in range(3):
        assert demo.run("explore something")["result"]["model_calls"] == 2
    capped = demo.run("explore something")["result"]
    assert (capped["success"], capped["model_calls"], capped["stop_reason"]) == (False, 0, "explore_limit")
    assert demo.run("explore something", ip="203.0.113.99")["result"]["model_calls"] == 2
    assert demo.allowances == [8, 8, 8, 0, 8]

    demo.clock.at += timedelta(minutes=59)
    assert demo.run("explore something")["result"]["model_calls"] == 0
    demo.clock.at += timedelta(minutes=2)
    assert demo.run("explore something")["result"]["model_calls"] == 2


def test_map_only_runs_are_never_limited():
    """R-COST-3: runs with zero model calls do not count, and still work for a capped IP."""
    demo = Demo(demo_model_runs_per_ip_hour=1, demo_max_explore_steps=5)
    for _ in range(6):
        assert demo.run("known task")["result"]["success"] is True
    assert demo.run("explore something")["result"]["model_calls"] == 2
    assert demo.run("known task")["result"]["success"] is True
    assert demo.run("known task")["result"]["success"] is True
    assert demo.allowances == [5] * 7 + [0, 0]


def test_spoofing_the_forwarded_header_does_not_reset_the_cap():
    """R-COST-3: only the proxy-appended (last) address counts; a client-chosen first entry is ignored."""
    demo = Demo(demo_model_runs_per_ip_hour=1)
    demo.run("explore something")
    headers = {"X-Forwarded-For": "1.2.3.4, 203.0.113.5"}
    response = demo.client.post("/demo/run", json={"goal_text": "explore", "params": {}}, headers=headers)
    assert response.status_code == 200
    assert demo.allowances == [8, 0]


def test_exhausted_budget_means_map_only_and_never_a_500():
    """R-COST-2, R-COST-3: with the budget exhausted every run is map-only; a budget error from
    the run itself is a short error event, not a 500."""
    demo = Demo()
    demo.budget = "exhausted"
    result = demo.run("explore something")["result"]
    assert (result["success"], result["model_calls"]) == (False, 0)
    assert demo.run("known task")["result"]["success"] is True
    assert demo.allowances == [0, 0]

    demo.budget, demo.fail = "ok", True
    events = demo.run("explore something")
    assert "result" not in events
    assert 0 < len(events["error"]["message"]) <= 120


@pytest.mark.parametrize("status", ["ok", "low", "exhausted"])
def test_config_shows_only_a_coarse_budget_state(status):
    """R-COST-3: /demo/config says ok, low or exhausted and the limits; never an amount or the key."""
    demo = Demo(model_budget_usd=1.5)
    demo.budget = status
    response = demo.client.get("/demo/config")
    body = response.json()
    assert body["budget"] == status
    assert (body["model_runs_per_ip_hour"], body["max_explore_steps"]) == (3, 8)
    text = response.text.lower()
    assert KEY not in response.text and "usd" not in text and "1.5" not in text and "$" not in text
