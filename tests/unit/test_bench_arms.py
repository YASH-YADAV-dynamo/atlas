"""Benchmark arms B0, B1 and Atlas on a scripted site with a deterministic model.

Targets (lane B), all returning a TaskRecord whose `success` is `ctx.check(answer)`:
    eval.arms.ArmContext(run_id, seed, iteration, split, driver, model, check,
                         routes=None, cache=None, secrets=None, max_steps=25)   # frozen dataclass
    eval.arms.TrajectoryCache()  .get(template_id) -> tuple[Action, ...] | None
                                 .put(template_id, actions: Sequence[Action]) -> None
    eval.arms.run_b0(task: BenchTask, ctx) / run_b1(task, ctx) / run_atlas(task, ctx)
The Atlas arm goes through atlas.runner.run.run_task and the RouteApi in `ctx.routes`.
The site starts where the task's `start_path` says (the arm navigates there first).
"""

from __future__ import annotations

import pytest

pytest.importorskip("eval.arms", reason="benchmark wave: implemented by lane B")
pytest.importorskip("atlas.runner.run", reason="needs the reference runner (lane S3)")

from atlas.contracts.bench_models import Arm, BenchTask, Split, TaskRecord
from atlas.contracts.types import TaskSpec
from eval.arms import ArmContext, TrajectoryCache, run_atlas, run_b0, run_b1
from tests.support.builders import SITE, click
from tests.support.fake_site import FakeSite
from tests.support.oracle import INPUT_TOKENS, OUTPUT_TOKENS, LocalRouteApi, OracleModel

REFUND_STEPS = 5  # Orders, Search (with fill), View order, Refund order, Confirm refund


def refund_task(order_id: str, start_path: str = "/", template_id: str = "refund_order") -> BenchTask:
    spec = TaskSpec(
        site=SITE, goal_text=f"Refund order {order_id}", params={"order_id": order_id}, start_path=start_path
    )
    return BenchTask(template_id=template_id, spec=spec)


def run(arm_fn, task: BenchTask, *, routes=None, cache=None) -> tuple[TaskRecord, FakeSite, OracleModel]:
    site, model = FakeSite(), OracleModel()
    order = task.spec.params["order_id"]
    ctx = ArmContext(
        run_id="unit",
        seed=7,
        iteration=1,
        split=Split.STREAM,
        driver=site,
        model=model,
        check=lambda answer: order in site.refunded,
        routes=routes,
        cache=cache,
    )
    return arm_fn(task, ctx), site, model


def test_b0_asks_the_model_for_every_step(fake_store, ns):
    """R-BENCH-1: B0 has no memory: one model call per action plus the call that says done."""
    for order in ("1042", "77"):
        record, site, model = run(run_b0, refund_task(order))
        assert (record.arm, record.success, record.actions) == (Arm.B0, True, REFUND_STEPS)
        assert record.model_calls == model.calls == REFUND_STEPS + 1
        assert (record.input_tokens, record.output_tokens) == (
            INPUT_TOKENS * record.model_calls,
            OUTPUT_TOKENS * record.model_calls,
        )
        assert (record.tool_calls, record.context_tokens) == (0, 0)
        assert (record.run_id, record.seed, record.iteration, record.split) == ("unit", 7, 1, Split.STREAM)
        assert record.template_id == "refund_order"
        assert site.refunded == {order}


def test_b1_replays_an_exact_template_match_with_new_parameters(fake_store, ns):
    """R-BENCH-1: the trajectory cache replays a stored action list for the same template
    (zero model calls) and has nothing for a template it has not seen."""
    cache = TrajectoryCache()
    first, _, _ = run(run_b1, refund_task("1042"), cache=cache)
    assert (first.arm, first.success, first.model_calls) == (Arm.B1, True, REFUND_STEPS + 1)
    assert len(cache.get("refund_order") or ()) == REFUND_STEPS

    replayed, site, model = run(run_b1, refund_task("77"), cache=cache)
    assert (replayed.success, replayed.model_calls, model.calls) == (True, 0, 0)
    assert (replayed.actions, site.refunded, site.typed) == (REFUND_STEPS, {"77"}, ["77"])

    other, _, model = run(run_b1, refund_task("88", template_id="refund_from_elsewhere"), cache=cache)
    assert (other.success, other.model_calls) == (True, REFUND_STEPS + 1)
    assert cache.get("never_seen") is None


def test_b1_falls_back_to_the_model_when_the_replay_breaks(fake_store, ns):
    """R-BENCH-1: a cached script whose step no longer exists on the page fails, the arm
    falls back to B0 from where it stands, and the record shows the full model cost."""
    cache = TrajectoryCache()
    run(run_b1, refund_task("1042"), cache=cache)
    cache.put("refund_order", [click("Does not exist"), *(cache.get("refund_order") or ())])
    record, site, model = run(run_b1, refund_task("77"), cache=cache)
    assert record.success is True
    assert record.model_calls == model.calls == REFUND_STEPS + 1
    assert site.refunded == {"77"}


def test_atlas_learns_once_then_drives_from_the_map(fake_store, ns):
    """R-BENCH-1, R-RUN-1: the Atlas arm explores once through the Route API; the next task of
    the type needs no model call."""
    routes = LocalRouteApi(fake_store, ns)
    first, _, _ = run(run_atlas, refund_task("1042"), routes=routes)
    assert (first.arm, first.success) == (Arm.ATLAS, True)
    assert first.model_calls >= REFUND_STEPS
    assert {"locate", "plan", "report"} <= set(routes.calls)

    second, site, model = run(run_atlas, refund_task("77"), routes=routes)
    assert (second.success, second.model_calls, model.calls) == (True, 0, 0)
    assert (second.actions, second.input_tokens, site.refunded) == (REFUND_STEPS, 0, {"77"})


def test_atlas_composes_a_new_start_with_a_known_route(fake_store, ns):
    """R-COMP-1: from a start page never used for this task, one explored step joins the
    learned route and the rest is driven from the map."""
    routes = LocalRouteApi(fake_store, ns)
    run(run_atlas, refund_task("1042"), routes=routes)
    record, site, model = run(run_atlas, refund_task("88", start_path="/settings"), routes=routes)
    assert (record.success, record.model_calls, model.calls) == (True, 1, 1)
    assert (record.actions, site.refunded) == (REFUND_STEPS, {"88"})

    again, site, model = run(run_atlas, refund_task("99", start_path="/settings"), routes=routes)
    assert (again.success, again.model_calls, site.refunded) == (True, 0, {"99"})
