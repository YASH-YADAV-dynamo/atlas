"""Cost guard in the flows (lane G): a spent budget or an exploration limit ends a run cleanly.

Targets:
    atlas.runner.run.run_task(..., max_model_calls: int | None = None)
        RunResult.stop_reason: "" | "budget_exceeded" | "explore_limit" | "step_limit"
"""

from __future__ import annotations

import inspect

import pytest

pytest.importorskip("atlas.runner.run", reason="needs the reference runner")

from atlas.contracts import errors
from atlas.contracts.types import TaskSpec
from atlas.runner.run import run_task
from tests.support.builders import SITE
from tests.support.fake_site import FakeSite
from tests.support.oracle import LocalRouteApi, OracleModel

if "max_model_calls" not in inspect.signature(run_task).parameters:
    pytest.skip("cost guard: runner limits implemented by lane G", allow_module_level=True)


class BrokeAfter:
    """A model whose budget runs out after `allowed` calls."""

    def __init__(self, allowed: int) -> None:
        self.inner, self.allowed, self.attempts = OracleModel(), allowed, 0

    def next_action(self, goal_text, params, snapshot, history):
        self.attempts += 1
        if self.attempts > self.allowed:
            raise errors.BudgetExceededError("model budget exhausted")
        return self.inner.next_action(goal_text, params, snapshot, history)


def refund(store, ns, site, order, model, **options):
    spec = TaskSpec(site=SITE, goal_text=f"Refund order {order}", params={"order_id": order})
    return run_task(spec, LocalRouteApi(store, ns), site, model, **options)


def test_runner_ends_cleanly_when_the_budget_runs_out(fake_store, ns):
    """R-COST-2: BudgetExceededError from the model ends the run without an exception: not
    successful, reason stated, steps so far kept and learned, the goal not linked."""
    site, model = FakeSite(), BrokeAfter(2)
    result = refund(fake_store, ns, site, "1042", model)
    assert (result.success, result.stop_reason) == (False, "budget_exceeded")
    assert (result.model_calls, len(result.steps), site.acts) == (2, 2, 2)
    assert len(fake_store.get_map(ns, SITE).transitions) == 2
    assert site.refunded == set()

    result = refund(fake_store, ns, FakeSite(), "1042", BrokeAfter(0))
    assert (result.success, result.stop_reason, result.model_calls) == (False, "budget_exceeded", 0)
    assert result.steps == ()


def test_exploration_is_capped_per_task(fake_store, ns):
    """R-COST-3: max_model_calls bounds model calls in one run; a learned task needs none."""
    site, model = FakeSite(), OracleModel()
    result = refund(fake_store, ns, site, "1042", model, max_model_calls=3)
    assert (result.success, result.stop_reason, result.model_calls, model.calls) == (
        False,
        "explore_limit",
        3,
        3,
    )
    assert site.refunded == set()

    refund(fake_store, ns, FakeSite(), "1042", OracleModel())
    site, model = FakeSite(), OracleModel()
    result = refund(fake_store, ns, site, "77", model, max_model_calls=0)
    assert (result.success, result.stop_reason, model.calls, site.refunded) == (True, "", 0, {"77"})
