"""Self-repair in the reference runner (SPEC 4.4). Targets atlas.runner.run.run_task on a
scripted site, the in-process Route API and a deterministic model.
"""

from __future__ import annotations

import pytest

pytest.importorskip("atlas.runner.run", reason="needs the reference runner")

from atlas.contracts.types import RunResult, StepSource, TaskSpec
from atlas.runner.run import run_task
from tests.support.builders import SITE
from tests.support.fake_site import FakeSite
from tests.support.oracle import LocalRouteApi, OracleModel

RENAMED = {"Refund order": "Issue refund"}


def refund(store, ns, site: FakeSite, order: str, **options) -> tuple[RunResult, OracleModel]:
    model = OracleModel()
    spec = TaskSpec(site=SITE, goal_text=f"Refund order {order}", params={"order_id": order})
    return run_task(spec, LocalRouteApi(store, ns), site, model, **options), model


def counts(store, ns, name: str) -> list[tuple[int, int]]:
    transitions = store.get_map(ns, SITE).transitions
    return sorted((t.ok, t.fail) for t in transitions if t.action.locator and t.action.locator.name == name)


def sources(result: RunResult) -> list[str]:
    return [step.source.value for step in result.steps]


def test_mismatch_penalises_the_edge_and_replans_from_the_actual_page(fake_store, ns):
    """R-REPLAN-1: after a rename the run drives the still-valid steps, records one failure on
    exactly the edge that lied, explores one step with the model, and finishes from the map."""
    refund(fake_store, ns, FakeSite(), "1042")
    site = FakeSite(renamed=RENAMED)
    result, model = refund(fake_store, ns, site, "77")
    assert (result.success, site.refunded) == (True, {"77"})
    assert (result.replans, result.model_calls, model.calls) == (1, 1, 1)
    assert sources(result) == ["map", "map", "map", "model", "map"]
    assert counts(fake_store, ns, "View order") == [(1, 0), (1, 1)]
    assert counts(fake_store, ns, "Orders") == [(2, 0)]

    after = FakeSite(renamed=RENAMED)
    result, model = refund(fake_store, ns, after, "88")
    assert (result.success, result.model_calls, result.replans, after.refunded) == (True, 0, 0, {"88"})


def test_a_mapped_step_that_cannot_execute_is_penalised(fake_store, ns):
    """R-REPLAN-1: a planned step whose control does not respond records a failure on that
    transition (it is not silently dropped), and the run ends unsuccessful without looping."""
    refund(fake_store, ns, FakeSite(), "1042")
    site = FakeSite(dead=frozenset({"Confirm refund"}))
    result, _ = refund(fake_store, ns, site, "77", max_steps=12)
    assert (result.success, site.refunded) == (False, set())
    assert len(result.steps) <= 12
    (confirm,) = counts(fake_store, ns, "Confirm refund")
    assert confirm[0] == 1 and confirm[1] >= 1
    dead_map_steps = [s for s in result.steps if s.source is StepSource.MAP and not s.ok]
    assert 1 <= len(dead_map_steps) <= 2


def test_a_run_that_did_not_reach_the_goal_never_links_it(fake_store, ns):
    """R-REPLAN-2: when the model gives up or the step limit is hit, no goal link is written,
    so a failed run cannot teach the map a wrong route."""
    site = FakeSite(dead=frozenset({"Confirm refund"}))
    result, _ = refund(fake_store, ns, site, "55", max_steps=8)
    assert result.success is False
    from atlas.core.ids import goal_id

    assert fake_store.goal_targets(ns, goal_id(ns, SITE, "refund order {order_id}")) == ()

    healthy = FakeSite()
    result, model = refund(fake_store, ns, healthy, "56")
    assert (result.success, healthy.refunded) == (True, {"56"})
    assert model.calls >= 1
