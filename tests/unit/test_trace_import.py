"""Trace import (SPEC 3b.3). Targets (lane T2), in atlas/import_trace.py:

parse_trace(lines: Iterable[str]) -> Iterator[TraceStep]
    one TraceStep per non-blank line; TraceFormatError(line=<1-based>) on bad JSON or schema
import_trace(routes: RouteApi, steps: Iterable[TraceStep]) -> ImportSummary
    per step: locate `before`, report the action with event id `trace_event_id(step)`, the
    step's run_id, params and goal fields; nothing else touches the map
TraceRecorder(driver: BrowserDriver, *, site, run_id, goal_text=None, params=None)
    a BrowserDriver that records every `act` (page before, action, page after, ok);
    .steps(goal_reached: bool) -> tuple[TraceStep, ...]
    .write(path: Path, goal_reached: bool) -> None      appends JSON lines
main(argv: Sequence[str], routes: RouteApi | None = None) -> int
    `python -m atlas.import_trace FILE.jsonl`; prints the ImportSummary as JSON; 0 on
    success, 2 on a format error (nothing imported); `routes` defaults to the HTTP API
    from settings.
"""

from __future__ import annotations

import itertools
import json

import pytest

pytest.importorskip("atlas.import_trace", reason="increment 8: implemented by lane T2")

from atlas.contracts.api_models import PlanRequest
from atlas.contracts.errors import TraceFormatError
from atlas.contracts.trace_models import ImportSummary, TraceStep, trace_event_id
from atlas.contracts.types import PlanReason
from atlas.core.planner import plan
from atlas.import_trace import TraceRecorder, import_trace, main, parse_trace
from tests.support.builders import SITE, state_id_of
from tests.support.fake_site import FakeSite
from tests.support.oracle import LocalRouteApi, OracleModel


def record_refund(order: str, run_id: str, site: FakeSite | None = None) -> TraceRecorder:
    """Another agent (the oracle) does a refund on its own browser while a recorder watches."""
    params = {"order_id": order}
    goal = f"Refund order {order}"
    recorder = TraceRecorder(site or FakeSite(), site=SITE, run_id=run_id, goal_text=goal, params=params)
    brain = OracleModel()
    while True:
        decision = brain.next_action(goal, params, recorder.snapshot(), ())
        if decision.done:
            return recorder
        recorder.act(decision.action)


def test_recorder_writes_one_schema_line_per_step(tmp_path):
    """R-TRACE-1: a recorded run is JSON lines, one TraceStep each, with the page before and
    after every action; the last step carries goal_reached."""
    path = tmp_path / "runs.jsonl"
    record_refund("1042", "agent-run-0001").write(path, goal_reached=True)
    record_refund("1043", "agent-run-0002").write(path, goal_reached=True)
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 10
    steps = list(parse_trace(lines))
    assert [json.loads(line) for line in lines] == [s.model_dump(mode="json") for s in steps]
    first_run = steps[:5]
    assert [s.index for s in first_run] == [0, 1, 2, 3, 4]
    assert {s.run_id for s in first_run} == {"agent-run-0001"}
    assert [s.goal_reached for s in first_run] == [False, False, False, False, True]
    assert first_run[1].action.fills[0].value == "1042"
    assert all(a.after == b.before for a, b in itertools.pairwise(first_run))
    assert first_run[0].before.url.endswith("/") and "/orders" in first_run[0].after.url


def test_bad_lines_are_reported_with_their_line_number():
    """R-TRACE-1: malformed JSON or a line that does not fit the schema raises TraceFormatError
    naming the line; blank lines are ignored."""
    (good,) = record_refund("1042", "agent-run-0001").steps(goal_reached=False)[:1]
    line = good.model_dump_json()
    assert list(parse_trace([line, "", "   ", line])) == [good, good]
    for bad in ("{not json", json.dumps({"run_id": "x"}), json.dumps({**json.loads(line), "extra": 1})):
        with pytest.raises(TraceFormatError) as caught:
            list(parse_trace([line, "", bad]))
        assert caught.value.line == 3


def test_imported_runs_become_a_map_that_serves_new_parameters(fake_store, ns):
    """R-TRACE-2: after importing one recorded run, a different order is planned end to end
    from the map: no exploration, no model."""
    routes = LocalRouteApi(fake_store, ns)
    steps = record_refund("1042", "agent-run-0001").steps(goal_reached=True)
    summary = import_trace(routes, steps)
    assert summary == ImportSummary(
        steps=5, runs=1, new_states=6, duplicates=0, failed_steps=0, goals_linked=1
    )

    view = fake_store.get_map(ns, SITE)
    assert (len(view.states), len(view.transitions)) == (6, 5)
    assert all((t.ok, t.fail) == (1, 0) for t in view.transitions)
    home = state_id_of(fake_store, ns, FakeSite().snapshot())
    result = plan(
        fake_store,
        ns,
        PlanRequest(site=SITE, state_id=home, goal_text="Refund order 77", params={"order_id": "77"}),
    )
    assert (result.reason, len(result.route)) == (PlanReason.ROUTE_FOUND, 5)
    assert result.route[1].action.fills[0].param == "order_id"
    assert "1042" not in result.model_dump_json()


def test_reimport_counts_nothing_twice(fake_store, ns):
    """R-TRACE-3: importing the same trace again changes no counter (stable event ids)."""
    routes = LocalRouteApi(fake_store, ns)
    steps = record_refund("1042", "agent-run-0001").steps(goal_reached=True)
    import_trace(routes, steps)
    again = import_trace(routes, steps)
    assert (again.steps, again.duplicates, again.new_states) == (5, 5, 0)
    assert all((t.ok, t.fail) == (1, 0) for t in fake_store.get_map(ns, SITE).transitions)
    assert trace_event_id(steps[3]) == "trace-agent-run-0001-0003"
    assert fake_store.has_event(ns, "trace-agent-run-0001-0003")


def test_failed_recorded_steps_teach_no_edge(fake_store, ns):
    """R-TRACE-3, R-LEARN-5: a step the recorded agent failed is counted but adds no transition,
    and a run recorded as not reaching its goal links none."""
    site = FakeSite(dead=frozenset({"Refund order"}))
    recorder = TraceRecorder(
        site, site=SITE, run_id="agent-run-0009", goal_text="Refund order 5", params={"order_id": "5"}
    )
    brain = OracleModel()
    for _ in range(4):
        action = brain.next_action("Refund order 5", {"order_id": "5"}, recorder.snapshot(), ()).action
        try:
            recorder.act(action)
        except Exception as error:  # the recorder re-raises the driver's error after recording
            assert type(error).__name__ == "BrowserActionError"
    steps = recorder.steps(goal_reached=False)
    assert [s.ok for s in steps] == [True, True, True, False]
    assert steps[3].after == steps[3].before
    summary = import_trace(LocalRouteApi(fake_store, ns), steps)
    assert (summary.steps, summary.failed_steps, summary.goals_linked) == (4, 1, 0)
    assert len(fake_store.get_map(ns, SITE).transitions) == 3


def test_command_line_imports_a_file(fake_store, ns, tmp_path, capsys):
    """R-TRACE-4: `python -m atlas.import_trace FILE` imports and prints the summary; a bad file
    imports nothing and exits 2."""
    path = tmp_path / "runs.jsonl"
    record_refund("1042", "agent-run-0001").write(path, goal_reached=True)
    routes = LocalRouteApi(fake_store, ns)
    assert main([str(path)], routes) == 0
    printed = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert ImportSummary.model_validate(printed).steps == 5
    assert len(fake_store.get_map(ns, SITE).transitions) == 5

    bad = tmp_path / "bad.jsonl"
    good_line = path.read_text(encoding="utf-8").splitlines()[0].replace("agent-run-0001", "agent-run-0002")
    bad.write_text(good_line + "\n{broken\n", encoding="utf-8")
    calls_before = len(routes.calls)
    assert main([str(bad)], routes) == 2
    assert len(routes.calls) == calls_before
    assert main([str(tmp_path / "missing.jsonl")], routes) == 2


def test_trace_step_schema_is_closed_and_bounded():
    """R-TRACE-1: the published schema rejects unknown fields and reuses the Route API types."""
    schema = TraceStep.model_json_schema()
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"run_id", "index", "site", "before", "action", "after"}
