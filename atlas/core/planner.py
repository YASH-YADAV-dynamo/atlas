"""Planner: cheapest route to the action that achieves a goal, widening the step bound."""

from __future__ import annotations

import math

from atlas.contracts.api_models import PlanRequest
from atlas.contracts.errors import UnknownStateError
from atlas.contracts.ports import GraphStore
from atlas.contracts.types import MAX_ROUTE_STEPS, PlanReason, PlanResult, RouteStep, StoredPath
from atlas.core.cost import reliability
from atlas.core.ids import goal_id
from atlas.core.params import goal_key, required_slots


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
    key = goal_key(request.goal_text, request.params)
    targets = store.goal_targets(ns, goal_id(ns, request.site, key))
    if not targets:
        return _needs_model(PlanReason.UNKNOWN_GOAL, 0)

    cap = max(1, min(max_steps_cap, MAX_ROUTE_STEPS))
    bound = max(1, min(max_steps, cap))
    while True:
        paths = _paths_within(store, ns, request.state_id, targets, bound, path_count)
        if paths or bound >= cap:
            break
        bound = min(bound * 2, cap)
    if not paths:
        return _needs_model(PlanReason.NO_ROUTE_WITHIN_MAX_STEPS, bound)

    for path in paths:
        route = _route(path)
        if all(required_slots(step.action) <= request.params.keys() for step in route):
            confidence = math.prod(step.reliability for step in route)
            return PlanResult(
                route=route,
                confidence=confidence,
                needs_model=False,
                reason=PlanReason.ROUTE_FOUND,
                max_steps_used=bound,
            )
    return _needs_model(PlanReason.MISSING_PARAMS, bound)
