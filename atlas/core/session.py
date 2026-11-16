"""Session core (SHIP_PLAN 3.4, 6): `start` and `finish`, the two calls of one task.

`start` locates the page, resolves the goal and plans in one round trip, and adds what the
answer memo knows: `open_url` (a read goal's answer page, or its URL template bound to this
task's parameters) and `search_url`. `finish` reports the whole outcome in one call: a replayed
plan (`plan_id` and `done` credit the issued route), explored steps, and the final page; a
reached read goal also remembers the answer page's address (never its content).

Issued plans live in memory for 15 minutes (`IssuedPlans`), like the pending form fills: a
restart inside that window costs one uncredited replay, nothing else.
"""

from __future__ import annotations

import secrets
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass

from atlas.contracts.api_models import (
    FinishRequest,
    FinishResponse,
    PlanRequest,
    StartRequest,
    StartResponse,
)
from atlas.contracts.errors import UnknownPlanError
from atlas.contracts.ports import Clock, SessionStore
from atlas.contracts.types import (
    MAX_ROUTE_STEPS,
    Fill,
    Goal,
    Observation,
    PlanReason,
    ReportEvent,
    ReportResult,
    RouteStep,
    Snapshot,
)
from atlas.core import goals, learner, memo, planner, replay
from atlas.core import locate as locating
from atlas.core.blocked import blocked_reason, unlearned_result
from atlas.core.ids import goal_id, transition_id
from atlas.core.params import goal_key
from atlas.log import log_event
from atlas.mcp.pending import as_fill, fold

ISSUED_TTL_SECONDS = 15 * 60
MAX_ISSUED = 10_000
_MAX_EVENT_BASE = 70  # leaves room for "-<step>" inside the 80-character event id limit


@dataclass(frozen=True)
class IssuedPlan:
    """A route or address handed out by `start`, kept to credit it when `finish` reports back."""

    plan_id: str
    ns: str
    site: str
    from_state: int
    goal: str  # the caller's goal key
    params: tuple[tuple[str, str], ...]
    route: tuple[RouteStep, ...]


class IssuedPlans:
    """Process-wide, bounded in size and age, keyed by (namespace, site, plan id)."""

    def __init__(self, ttl: float = ISSUED_TTL_SECONDS, max_items: int = MAX_ISSUED) -> None:
        self._ttl = ttl
        self._max = max_items
        self._lock = threading.Lock()
        self._items: OrderedDict[tuple[str, str, str], tuple[float, IssuedPlan]] = OrderedDict()

    def issue(
        self,
        ns: str,
        site: str,
        from_state: int,
        goal: str,
        params: dict[str, str],
        route: tuple[RouteStep, ...],
    ) -> str:
        plan_id = secrets.token_hex(6)  # 12 characters
        plan = IssuedPlan(plan_id, ns, site, from_state, goal, tuple(sorted(params.items())), route)
        now = time.monotonic()
        with self._lock:
            self._expire(now)
            self._items[(ns, site, plan_id)] = (now, plan)
            while len(self._items) > self._max:
                self._items.popitem(last=False)
        return plan_id

    def get(self, ns: str, site: str, plan_id: str) -> IssuedPlan | None:
        now = time.monotonic()
        with self._lock:
            self._expire(now)
            entry = self._items.get((ns, site, plan_id))
        return entry[1] if entry else None

    def _expire(self, now: float) -> None:
        while self._items:
            first = next(iter(self._items))
            if now - self._items[first][0] <= self._ttl:
                return
            del self._items[first]


ISSUED = IssuedPlans()


# ---- start -------------------------------------------------------------------------------------


def start(
    store: SessionStore,
    ns: str,
    request: StartRequest,
    *,
    read_only: bool = False,
    max_steps: int = MAX_ROUTE_STEPS,
    max_steps_cap: int = MAX_ROUTE_STEPS,
    path_count: int = 5,
    issued: IssuedPlans = ISSUED,
    final_step_varies: Callable[[tuple[int, ...], tuple[RouteStep, ...]], bool] | None = None,
    script_fits: Callable[[str], bool] | None = None,
) -> StartResponse:
    """Locate, match the goal, plan, and look up the memo. A read-only key stores nothing.

    `final_step_varies(targets, route)`: true when the route's last step is a literal pick that
    differed between earlier successful runs, so it is cut (MCP uses it).
    `script_fits`: the caller's size check for a script.
    """
    blocked = blocked_reason(request.snapshot)
    if blocked is not None:  # never stored, never matched to a known page
        seen = unlearned_result(store, ns, request.site, request.snapshot, request.params, blocked)
        return StartResponse(
            state_id=seen.state_id,
            known=seen.known,
            needs_model=True,
            reason=PlanReason.NO_ROUTE_WITHIN_MAX_STEPS,
            blocked=blocked,
        )
    resolution = locating.resolve(store, ns, request.site, request.snapshot, request.params)
    located = resolution.result() if read_only else locating.commit(store, ns, resolution)

    found = goals.resolve(store, ns, request.site, request.goal_text, request.params)
    params = dict(found.params) if found.inferred else dict(request.params)
    plan_request = PlanRequest(
        site=request.site,
        state_id=located.state_id,
        goal_text=request.goal_text,
        params=params,
        snapshot=request.snapshot,
    )
    result = planner.plan_for(
        store,
        ns,
        plan_request,
        found,
        max_steps=max_steps,
        max_steps_cap=max_steps_cap,
        path_count=path_count,
    )

    route = result.route
    needs_model, reason = result.needs_model, result.reason
    if route and final_step_varies is not None and final_step_varies(found.targets, route):
        route = route[:-1]  # replaying one of the earlier literal picks would be wrong
        needs_model, reason = True, PlanReason.MISSING_PARAMS

    key = goal_key(request.goal_text, params)
    candidates = [goal_id(ns, request.site, key)]
    if found.automatic and found.goal is not None and found.goal != key:
        candidates.append(goal_id(ns, request.site, found.goal))
    remembered = memo.recall(store, ns, request.site, candidates, params)
    open_url = remembered[0] if remembered else None
    match, goal = result.match, result.goal
    if remembered is not None and remembered[1] == 0:  # the caller's own wording has a memo
        match, goal = "exact", None

    served = open_url is not None or (bool(route) and not needs_model)
    search_url = None
    if not route and open_url is None and reason is PlanReason.UNKNOWN_GOAL:
        search_url = memo.search_entry(store, ns, request.site)
    issuing = bool(route) or open_url is not None
    plan_id = issued.issue(ns, request.site, located.state_id, key, params, route) if issuing else None
    script = None
    if request.replay == "script" and route and not needs_model:
        script = replay.build_script(route, params, fits=script_fits)
    return StartResponse(
        state_id=located.state_id,
        known=located.known,
        matched=located.matched,
        plan_id=plan_id,
        match=match,
        goal=goal,
        params=result.params,
        route=() if script is not None else route,
        confidence=result.confidence,
        open_url=open_url,
        search_url=search_url,
        script=script.text if script is not None else None,
        script_steps=script.steps if script is not None else None,
        needs_model=not served,
        reason=PlanReason.ROUTE_FOUND if served else reason,
        max_steps_used=result.max_steps_used,
    )


# ---- finish ------------------------------------------------------------------------------------


@dataclass
class _Run:
    """What one `finish` call has done so far."""

    state: int | None = None
    learned: int = 0
    last_transition: int | None = None  # the last credited replay step, when it was the goal
    blocked: str | None = None


def _link_goal(store: SessionStore, ns: str, request: FinishRequest, transition: int) -> str | None:
    """Link the goal (and its same-words alias) to a transition the replay credited."""
    if request.goal_text is None:
        return None
    key = goal_key(request.goal_text, request.params)
    store.link_goal(ns, Goal(id=goal_id(ns, request.site, key), site=request.site, text=key), transition)
    words = goals.same_words_key(key)
    if words:
        store.link_goal(ns, goals.alias_goal(ns, request.site, words, request.kind), transition)
    return key


def _credit_replay(
    store: SessionStore,
    clock: Clock,
    ns: str,
    request: FinishRequest,
    plan: IssuedPlan,
    on_report: Callable[[ReportResult], None],
    run: _Run,
) -> None:
    """Steps 1..done of the issued route succeeded; when the run failed, the next one failed once."""
    route = plan.route
    done = request.done if request.done is not None else (len(route) if request.ok else 0)
    done = min(done, len(route))
    per_step_ms = request.ms // done if done else 0
    state = plan.from_state
    run.state = state
    for index, step in enumerate(route):
        ok = index < done
        if not ok and (request.ok or index > done):
            break
        event_id = f"{plan.plan_id}:{index}"
        duplicate = store.has_event(ns, event_id)
        transition = store.record_observation(
            ns,
            Observation(
                event_id=event_id,
                transition_id=transition_id(ns, state, step.action, step.expect_state),
                site=request.site,
                from_state=state,
                action=step.action,
                to_state=step.expect_state,
                ok=ok,
                ms=per_step_ms if ok else 0,
                at=clock.now(),
            ),
        )
        on_report(
            ReportResult(
                state_id=step.expect_state if ok else state,
                known=True,
                duplicate=duplicate,
                transitions=(transition,),
            )
        )
        if ok:
            state = step.expect_state
            run.state = state
            run.last_transition = transition.id
            run.learned += 0 if duplicate else 1


def _explore(
    store: SessionStore,
    clock: Clock,
    ns: str,
    request: FinishRequest,
    base: str,
    on_report: Callable[[ReportResult], None],
    run: _Run,
) -> None:
    """Report the explored steps in order. Standalone form fills are folded into the action that
    follows them from the same page (nothing is held between calls)."""
    state = request.from_state
    assert state is not None  # the request validator guarantees it
    held: list[Fill] = []
    last = len(request.steps) - 1
    for index, step in enumerate(request.steps):
        closing = index == last
        action = step.action
        blocked = blocked_reason(step.to)
        if step.ok and blocked is None:
            fill = None if closing and request.goal_reached else as_fill(action)
            if (
                fill is not None
                and locating.resolve(store, ns, request.site, step.to, request.params).state_id == state
            ):
                held = [f for f in held if f.locator != fill.locator] + [fill]
                continue
            action = fold(action, held)
            held = []
        event = ReportEvent(
            site=request.site,
            from_state=state,
            action=action,
            to_snapshot=step.to,
            ok=step.ok,
            ms=step.ms,
            event_id=f"{base}-{index}",
            goal_text=request.goal_text,
            params=request.params,
            goal_reached=closing and request.goal_reached and request.ok and step.ok,
            run_id=base,
            kind=request.kind,
        )
        result = learner.report(store, clock, ns, event)
        on_report(result)
        if result.blocked is not None:
            run.blocked = result.blocked
        else:
            state = result.state_id
            run.state = state
            if step.ok and not result.duplicate:
                run.learned += 1


def _ignore(_result: ReportResult) -> None:
    return None


def finish(
    store: SessionStore,
    clock: Clock,
    ns: str,
    request: FinishRequest,
    on_report: Callable[[ReportResult], None] | None = None,
    *,
    issued: IssuedPlans = ISSUED,
) -> FinishResponse:
    """Learn one task's outcome. `on_report` sees every stored step (quota and flow-change hooks)."""
    notify: Callable[[ReportResult], None] = on_report if on_report is not None else _ignore
    run = _Run(state=request.from_state)
    base = (request.event_id or uuid.uuid4().hex[:24])[:_MAX_EVENT_BASE]
    plan = issued.get(ns, request.site, request.plan_id) if request.plan_id else None
    if request.plan_id and plan is None:
        log_event("finish_unknown_plan")  # expired or never issued: nothing to credit
    if plan is not None:
        _credit_replay(store, clock, ns, request, plan, notify, run)
    if request.steps:
        _explore(store, clock, ns, request, base, notify, run)

    final_blocked = run.blocked
    final = request.to_snapshot
    if final is not None:
        reason = blocked_reason(final)
        if reason is not None:
            final_blocked = reason
            run.state = unlearned_result(store, ns, request.site, final, request.params, reason).state_id
        else:
            arrived = locating.locate(store, ns, request.site, final, request.params)
            run.state = arrived.state_id
    if run.state is None:  # a plan that expired, with nothing else to say where the caller is
        run.state = request.from_state
    if run.state is None:  # only an expired plan id: nothing was stored and there is no page to name
        raise UnknownPlanError(
            f"unknown or expired plan_id: {request.plan_id}; send from_state or to_snapshot"
        )

    reached = request.goal_reached and request.ok and final_blocked is None
    goal: str | None = None
    if reached and request.goal_text is not None:
        goal = goal_key(request.goal_text, request.params)
        if not request.steps and run.last_transition is not None:  # replay ended on the goal's step
            _link_goal(store, ns, request, run.last_transition)
        if request.kind == "read":
            _remember(store, clock, ns, request, final)
    return FinishResponse(
        state_id=run.state, learned=run.learned, goal=goal if reached else None, blocked=final_blocked
    )


def _remember(
    store: SessionStore, clock: Clock, ns: str, request: FinishRequest, final: Snapshot | None
) -> None:
    """A reached read goal: keep the answer page's address and the site's search entry."""
    assert request.goal_text is not None
    page = final
    if page is None and request.steps:
        last = request.steps[-1]
        page = last.to if last.ok and blocked_reason(last.to) is None else None
    if page is not None:
        key = goal_key(request.goal_text, request.params)
        memo.remember_answer(
            store,
            clock,
            ns,
            request.site,
            goal_id(ns, request.site, key),
            request.params,
            page.url,
            page.title,
        )
    urls = [s.to.url for s in request.steps if s.ok and blocked_reason(s.to) is None]
    if request.params and urls:
        memo.remember_search(store, clock, ns, request.site, urls, request.params)
