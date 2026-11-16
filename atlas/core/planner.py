"""Planner: cheapest route to the action that achieves a goal, widening the step bound."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping

from atlas.contracts.api_models import PlanRequest
from atlas.contracts.errors import MissingParamError, UnknownStateError
from atlas.contracts.ports import GraphStore
from atlas.contracts.types import (
    MAX_ROUTE_STEPS,
    Action,
    PlanReason,
    PlanResult,
    RouteStep,
    Snapshot,
    StoredPath,
)
from atlas.core.cost import reliability
from atlas.core.goals import GoalMatch, resolve
from atlas.core.params import bind_action, required_slots

_SPACES = re.compile(r"\s+")


def _needs_model(reason: PlanReason, steps_used: int) -> PlanResult:
    return PlanResult(needs_model=True, reason=reason, max_steps_used=steps_used)


def _paths_within(
    store: GraphStore, ns: str, source: int, targets: tuple[int, ...], bound: int, count: int
) -> list[StoredPath]:
    found = [p for target in targets for p in store.shortest_paths(ns, source, target, bound, count)]
    return sorted(found, key=lambda path: path.cost)


def _route(path: StoredPath) -> tuple[RouteStep, ...]:
    return tuple(
        RouteStep(
            action=t.action,
            expect_state=t.to_state,
            reliability=reliability(t.ok, t.fail),
            last_verified=t.last_ok_at,
        )
        for t in path.steps
    )


def _fold(text: str) -> str:
    return _SPACES.sub(" ", text).strip().lower()


def control_on_page(action: Action, snapshot: Snapshot | None, params: Mapping[str, str]) -> bool:
    """First-step guard: the action's control (role, and name as a case-insensitive substring,
    as browsers match accessible names) is on the page. Pages listed without elements, goto
    actions and unbound names are not judged here."""
    if snapshot is None or not snapshot.elements or action.locator is None:
        return True
    try:
        locator = bind_action(action, params).locator
    except MissingParamError:
        return True
    if locator is None:
        return True
    name = _fold(locator.name)
    return any(e.role == locator.role and name in _fold(e.name) for e in snapshot.elements)


def plan(
    store: GraphStore,
    ns: str,
    request: PlanRequest,
    *,
    max_steps: int = MAX_ROUTE_STEPS,
    max_steps_cap: int = MAX_ROUTE_STEPS,
    path_count: int = 5,
) -> PlanResult:
    if store.get_state(ns, request.state_id) is None:
        raise UnknownStateError(f"unknown state: {request.state_id}")
    found = resolve(store, ns, request.site, request.goal_text, request.params)
    return plan_for(
        store, ns, request, found, max_steps=max_steps, max_steps_cap=max_steps_cap, path_count=path_count
    )


def plan_for(
    store: GraphStore,
    ns: str,
    request: PlanRequest,
    found: GoalMatch,
    *,
    max_steps: int = MAX_ROUTE_STEPS,
    max_steps_cap: int = MAX_ROUTE_STEPS,
    path_count: int = 5,
) -> PlanResult:
    """Route for an already resolved goal (`atlas.core.goals.resolve`)."""
    matched: dict[str, object] = {
        "match": found.match,
        "goal": found.goal,
        "params": found.params if found.inferred else None,
    }
    if not found.automatic:  # unknown, or only offered (similar): the caller decides
        return _needs_model(PlanReason.UNKNOWN_GOAL, 0).model_copy(update=matched)
    params = found.params

    cap = max(1, min(max_steps_cap, MAX_ROUTE_STEPS))
    bound = max(1, min(max_steps, cap))
    while True:
        paths = _paths_within(store, ns, request.state_id, found.targets, bound, path_count)
        if paths or bound >= cap:
            break
        bound = min(bound * 2, cap)
    if not paths:
        return _needs_model(PlanReason.NO_ROUTE_WITHIN_MAX_STEPS, bound).model_copy(update=matched)

    off_page = False
    for path in paths:
        route = _route(path)
        if not control_on_page(route[0].action, request.snapshot, params):
            off_page = True
            continue
        if all(required_slots(step.action) <= params.keys() for step in route):
            confidence = math.prod(step.reliability for step in route)
            return PlanResult(
                route=route,
                confidence=confidence,
                needs_model=False,
                reason=PlanReason.ROUTE_FOUND,
                max_steps_used=bound,
            ).model_copy(update=matched)
    if off_page:  # no route starts with a control this page shows
        return _needs_model(PlanReason.NO_ROUTE_WITHIN_MAX_STEPS, bound).model_copy(update=matched)
    return _needs_model(PlanReason.MISSING_PARAMS, bound).model_copy(update=matched)
