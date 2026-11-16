"""Skills: path compression in the sleep phase (SPEC 5.1, ADR-0006). Targets (lane K):

atlas.core.sleep.skill_candidates(runs: Sequence[Sequence[RunStep]], keep_out: frozenset[int],
        *, min_steps: int = 3, min_runs: int = 3) -> tuple[tuple[tuple[int, ...], int], ...]
    (via, supporting runs) pairs, sorted by via. A run is cut into segments at every failed
    step and at every transition in `keep_out`; a segment of at least `min_steps` transitions
    that occurs, identically, in at least `min_runs` runs is a candidate.
atlas.core.sleep.run_sleep(store, clock: Clock, ns: str, site: str) -> SleepReport
    creates a skill per new candidate (id = stable_id(ns, "skill", *via), cost = 0.95 * the sum
    of its transitions' costs, keep_out = every goal's target transitions), removes a skill
    when one of its transitions' two newest outcomes are failures, and logs both.
learner.report passes ReportEvent.run_id on to the Observation; both stores implement SkillStore
and let shortest_paths ride skills (one hop each, steps expanded, skill_ids set).
POST /v1/sleep?site=  -> SleepReport         GET /v1/skills?site= -> SkillsResponse
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

pytest.importorskip("atlas.core.sleep", reason="increment 7: implemented by lane K")

from atlas.contracts.api_models import SKILLS_PATH, SLEEP_PATH, PlanRequest
from atlas.contracts.types import MAX_ROUTE_STEPS, PlanReason, RunStep, SleepLogKind
from atlas.core.planner import plan
from atlas.core.sleep import run_sleep, skill_candidates
from tests.support.builders import GOAL, SITE, TickClock, click, learn, page, state_id_of
from tests.support.scenarios import client_for

T0 = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)
NAMES = "abcdefghijklmnop"


def steps(run: str, *outcomes: tuple[int, bool]) -> list[RunStep]:
    return [
        RunStep(run_id=run, transition_id=tid, ok=ok, at=T0 + timedelta(seconds=i))
        for i, (tid, ok) in enumerate(outcomes)
    ]


def ok_run(run: str, *tids: int) -> list[RunStep]:
    return steps(run, *[(tid, True) for tid in tids])


def test_candidates_need_three_steps_in_three_runs():
    """R-SKILL-1: a path becomes a candidate only when at least 3 transitions long and driven
    successfully, identically, in at least 3 runs."""
    three = [ok_run(f"run-000{i}", 1, 2, 3, 4) for i in range(3)]
    assert skill_candidates(three, frozenset()) == (((1, 2, 3, 4), 3),)
    assert skill_candidates(three[:2], frozenset()) == ()
    assert skill_candidates([ok_run(f"run-000{i}", 1, 2) for i in range(5)], frozenset()) == ()
    five = [*three, ok_run("run-0007", 1, 2, 3, 4), ok_run("run-0008", 9, 8, 7)]
    assert skill_candidates(five, frozenset()) == (((1, 2, 3, 4), 4),)


def test_failures_and_goal_steps_cut_a_run_into_segments():
    """R-SKILL-1: a failed step never sits inside a skill, and a goal's final transition is
    never compressed away (the plan must still end by executing it)."""
    broken = [
        steps(f"run-000{i}", (1, True), (2, True), (3, False), (4, True), (5, True), (6, True))
        for i in range(3)
    ]
    assert skill_candidates(broken, frozenset()) == (((4, 5, 6), 3),)
    with_goal = [ok_run(f"run-000{i}", 1, 2, 3, 4, 5, 6, 7) for i in range(3)]
    assert skill_candidates(with_goal, frozenset({4})) == (((1, 2, 3), 3), ((5, 6, 7), 3))
    assert skill_candidates(with_goal, frozenset({7})) == (((1, 2, 3, 4, 5, 6), 3),)
    assert skill_candidates(with_goal, frozenset({3, 6})) == ()


def drive(store, ns, length: int, runs: int, prefix: str = "w") -> list:
    """`runs` runs over a chain of `length` steps; the last step achieves GOAL."""
    pages = [page(f"/{prefix}/{NAMES[i]}", f"button:go-{NAMES[i]}") for i in range(length + 1)]
    clock = TickClock()
    for run in range(runs):
        for index in range(length):
            last = index == length - 1
            learn(
                store,
                ns,
                pages[index],
                click(f"go-{NAMES[index]}"),
                pages[index + 1],
                goal_text=GOAL if last else None,
                goal_reached=last,
                run_id=f"{prefix}-run-{run:04d}",
                clock=clock,
            )
    return pages


def test_sleep_compresses_a_proven_path_once(fake_store, ns):
    """R-SKILL-2: after three runs the path before the goal step becomes one skill between its
    end states, priced just under the sum; sleeping again changes nothing; the change is logged."""
    pages = drive(fake_store, ns, 5, runs=3)
    report = run_sleep(fake_store, TickClock(), ns, SITE)
    (skill,) = report.created
    transitions = {t.id: t for t in fake_store.get_map(ns, SITE).transitions}
    assert [transitions[tid].action.locator.name for tid in skill.via] == ["go-a", "go-b", "go-c", "go-d"]
    assert (skill.from_state, skill.to_state) == (
        state_id_of(fake_store, ns, pages[0]),
        state_id_of(fake_store, ns, pages[4]),
    )
    assert skill.runs == 3
    assert skill.cost == pytest.approx(0.95 * sum(transitions[tid].cost for tid in skill.via))
    assert fake_store.list_skills(ns, SITE) == (skill,)
    (entry,) = fake_store.list_log(ns, SITE)
    assert (entry.kind, entry.skill_id, entry.via) == (SleepLogKind.SKILL_CREATED, skill.id, skill.via)
    assert entry.evidence

    again = run_sleep(fake_store, TickClock(), ns, SITE)
    assert (again.created, again.removed) == ((), ())
    assert len(fake_store.list_log(ns, SITE)) == 1
    assert run_sleep(fake_store, TickClock(), f"{ns}x", SITE).created == ()


def test_steps_without_a_run_id_never_become_skills(fake_store, ns):
    """R-SKILL-2: evidence without a run id cannot prove a path was driven end to end."""
    pages = [page(f"/n/{NAMES[i]}", f"button:go-{NAMES[i]}") for i in range(5)]
    for _ in range(4):
        for index in range(4):
            learn(fake_store, ns, pages[index], click(f"go-{NAMES[index]}"), pages[index + 1])
    assert run_sleep(fake_store, TickClock(), ns, SITE).created == ()
    assert fake_store.run_steps(ns, SITE) == ()


def test_planning_rides_the_skill_and_reaches_further(fake_store, ns):
    """R-SKILL-3, R-PLAN-7: a skill is one hop for the path query, so a 12-step flow that no
    single query could return becomes plannable, and the route is the full expanded step list."""
    pages = drive(fake_store, ns, 12, runs=3)
    request = PlanRequest(site=SITE, state_id=state_id_of(fake_store, ns, pages[0]), goal_text=GOAL)
    before = plan(fake_store, ns, request, max_steps=MAX_ROUTE_STEPS, max_steps_cap=MAX_ROUTE_STEPS)
    assert (before.route, before.reason) == ((), PlanReason.NO_ROUTE_WITHIN_MAX_STEPS)

    (skill,) = run_sleep(fake_store, TickClock(), ns, SITE).created
    assert len(skill.via) == 11
    after = plan(fake_store, ns, request, max_steps=MAX_ROUTE_STEPS, max_steps_cap=MAX_ROUTE_STEPS)
    assert after.reason is PlanReason.ROUTE_FOUND
    assert [s.action.locator.name for s in after.route] == [f"go-{NAMES[i]}" for i in range(12)]
    assert [s.expect_state for s in after.route] == [state_id_of(fake_store, ns, p) for p in pages[1:]]

    target = fake_store.goal_targets(ns, _goal(ns))[0]
    (path,) = fake_store.shortest_paths(ns, request.state_id, target, max_steps=2, path_count=3)
    assert (path.skill_ids, len(path.steps)) == ((skill.id,), 12)
    assert path.cost == pytest.approx(skill.cost + path.steps[-1].cost)
    midway = fake_store.shortest_paths(ns, state_id_of(fake_store, ns, pages[6]), target, 8, 3)
    assert [(p.skill_ids, len(p.steps)) for p in midway] == [((), 6)]


def _goal(ns: str) -> int:
    from atlas.core.ids import goal_id

    return goal_id(ns, SITE, GOAL)


def test_a_skill_is_removed_when_one_of_its_steps_breaks(fake_store, ns):
    """R-SKILL-4: two consecutive failures of a constituent transition delete the skill; the
    plain route is still there, the removal is logged, and the skill is not re-created while
    the step keeps failing."""
    pages = drive(fake_store, ns, 5, runs=3)
    (skill,) = run_sleep(fake_store, TickClock(), ns, SITE).created
    clock = TickClock(T0 + timedelta(hours=1))
    c_id = state_id_of(fake_store, ns, pages[2])
    learn(fake_store, ns, pages[1], click("go-b"), pages[1], ok=False, expect_state=c_id, clock=clock)
    assert run_sleep(fake_store, clock, ns, SITE).removed == ()
    learn(fake_store, ns, pages[1], click("go-b"), pages[1], ok=False, expect_state=c_id, clock=clock)

    report = run_sleep(fake_store, clock, ns, SITE)
    assert (report.removed, report.created) == ((skill.id,), ())
    assert fake_store.list_skills(ns, SITE) == ()
    kinds = [entry.kind for entry in fake_store.list_log(ns, SITE)]
    assert kinds == [SleepLogKind.SKILL_CREATED, SleepLogKind.SKILL_REMOVED]
    request = PlanRequest(site=SITE, state_id=state_id_of(fake_store, ns, pages[0]), goal_text=GOAL)
    assert len(plan(fake_store, ns, request, max_steps=8, max_steps_cap=8).route) == 5
    assert run_sleep(fake_store, clock, ns, SITE).created == ()


def test_sleep_and_skills_over_the_api(fake_store, ns):
    """R-SKILL-5: reports carrying run_id feed the sleep phase; POST /v1/sleep runs it and
    GET /v1/skills lists skills and the change log; both validate the site and need the key."""
    from tests.support.scenarios import post, report_body

    client = client_for(fake_store, ns, clock=TickClock())
    pages = [page(f"/api/{NAMES[i]}", f"button:go-{NAMES[i]}") for i in range(5)]
    ids = [
        post(client, "/v1/locate", {"site": SITE, "snapshot": p.model_dump(mode="json")})["state_id"]
        for p in pages
    ]
    for run in range(3):
        for index in range(4):
            body = report_body(
                ids[index], click(f"go-{NAMES[index]}"), pages[index + 1], run_id=f"api-run-{run:04d}"
            )
            post(client, "/v1/report", body)

    slept = client.post(SLEEP_PATH, params={"site": SITE})
    assert slept.status_code == 200, slept.text
    assert (len(slept.json()["created"]), slept.json()["removed"]) == (1, [])
    listed = client.get(SKILLS_PATH, params={"site": SITE}).json()
    assert (listed["site"], len(listed["skills"]), len(listed["log"])) == (SITE, 1, 1)
    assert len(listed["skills"][0]["via"]) == 4
    assert client.get(SKILLS_PATH, params={"site": "other.test"}).json()["skills"] == []
    assert client.post(SLEEP_PATH, params={"site": "x' DETACH DELETE"}).status_code == 422
    locked = client_for(fake_store, ns, api_key="test-key-not-a-secret")
    assert locked.post(SLEEP_PATH, params={"site": SITE}).status_code == 401
    assert locked.get(SKILLS_PATH, params={"site": SITE}).status_code == 401
