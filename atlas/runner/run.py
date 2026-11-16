"""Reference runner: locate -> plan -> drive route steps from the map -> ask the model only where
the map ends -> report everything. Uses the Route API port only (R-RUN-1).

Secrets (login) bind actions and templatise model actions; their values never leave this process:
requests carry the placeholder `{slot}` as the value, and logs / StepLogs hold templated actions.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable, Mapping

from atlas.contracts.api_models import LocateRequest, PlanRequest, ReportRequest
from atlas.contracts.errors import BrowserActionError, BudgetExceededError
from atlas.contracts.ports import BrowserDriver, ModelClient, RouteApi
from atlas.contracts.types import (
    Action,
    ActionKind,
    RunResult,
    Snapshot,
    StepLog,
    StepSource,
    TaskSpec,
)
from atlas.core.params import bind_action, templatise_action
from atlas.log import log_event

MAX_REPLANS = 1
MAX_SAME_FAILURES = 2  # a step that failed this often from one state is not tried again this run
HISTORY_LINES = 6


def describe(action: Action) -> str:
    """A history line with kinds and control names only, never values."""
    parts = [f"{f.kind.value} {f.locator.role} '{f.locator.name}'" for f in action.fills]
    if action.locator is not None:
        row = f" in row '{action.locator.row}'" if action.locator.row else ""
        parts.append(f"{action.kind.value} {action.locator.role} '{action.locator.name}'{row}")
    else:
        parts.append(f"{action.kind.value} {action.value or ''}")
    return "; ".join(parts)


def run_task(
    task: TaskSpec,
    routes: RouteApi,
    driver: BrowserDriver,
    model: ModelClient,
    *,
    secrets: Mapping[str, str] | None = None,
    max_steps: int = 25,
    max_model_calls: int | None = None,
    on_step: Callable[[StepLog], None] | None = None,
) -> RunResult:
    secrets = dict(secrets or {})
    bind_params = {**task.params, **secrets}
    wire_params = {**task.params, **{slot: f"{{{slot}}}" for slot in secrets}}  # hidden values
    task_id = uuid.uuid4().hex[:12]
    started = time.monotonic()
    steps: list[StepLog] = []
    history: list[str] = []
    model_calls = tokens = replans = 0
    answer = ""
    success = False
    last_event: ReportRequest | None = None
    failures: dict[tuple[int, str], int] = {}
    map_dead = False
    stop_reason = ""

    def failed_out(state: int, templated: Action) -> bool:
        return failures.get((state, templated.model_dump_json()), 0) >= MAX_SAME_FAILURES

    def ms_since(t0: float) -> int:
        return max(0, int((time.monotonic() - t0) * 1000))

    def execute(
        state: int, snap: Snapshot, templated: Action, source: StepSource, expect: int | None
    ) -> tuple[int, Snapshot, bool]:
        """Act, snapshot, report, log. Returns (state now, snapshot now, executed ok)."""
        nonlocal last_event
        index = len(steps)
        t0 = time.monotonic()
        try:
            driver.act(bind_action(templated, bind_params))
            ok = True
        except BrowserActionError:
            ok = False
        elapsed = ms_since(t0)
        after = driver.snapshot() if ok else snap
        event = ReportRequest(
            site=task.site,
            from_state=state,
            action=templated,
            to_snapshot=after,
            ok=ok,
            ms=min(elapsed, 600_000),
            event_id=f"{task_id}-s{index}",
            expect_state=expect,  # a dead mapped step is a failure of that transition
            goal_text=task.goal_text,
            params=wire_params,
            run_id=f"run-{task_id}",
        )
        response = routes.report(event)
        last_event = event
        new_state = response.state_id if ok else state
        if not ok:
            key = (state, templated.model_dump_json())
            failures[key] = failures.get(key, 0) + 1
        log = StepLog(
            index=index,
            source=source,
            action=templated,
            from_state=state,
            to_state=new_state if ok else None,
            ok=ok,
            ms=elapsed,
        )
        steps.append(log)
        history.append(f"{'ok' if ok else 'FAILED'}: {describe(templated)}")
        log_event("step", task_id, index=index, source=source.value, ok=ok, ms=elapsed)
        if on_step is not None:
            on_step(log)
        return new_state, after, ok

    driver.act(Action(kind=ActionKind.GOTO, value=task.start_path))
    snap = driver.snapshot()
    state = routes.locate(LocateRequest(site=task.site, snapshot=snap)).state_id
    log_event("start", task_id)

    while len(steps) < max_steps and not success:
        plan = routes.plan(
            PlanRequest(site=task.site, state_id=state, goal_text=task.goal_text, params=wire_params)
        )
        if plan.route and replans <= MAX_REPLANS and not map_dead:
            for step in plan.route:
                if len(steps) >= max_steps:
                    break
                if failed_out(state, step.action):
                    map_dead = True  # the map's road is dead here: leave the rest to the model
                    break
                new_state, snap, ok = execute(state, snap, step.action, StepSource.MAP, step.expect_state)
                if not ok or new_state != step.expect_state:
                    state = new_state
                    replans += 1
                    break
                state = new_state
                if step is plan.route[-1]:
                    success = True
            continue

        if max_model_calls is not None and model_calls >= max_model_calls:
            stop_reason = "explore_limit"
            break
        try:
            decision = model.next_action(task.goal_text, wire_params, snap, tuple(history[-HISTORY_LINES:]))
        except BudgetExceededError:
            stop_reason = "budget_exceeded"
            break
        model_calls += 1
        tokens += decision.input_tokens + decision.output_tokens
        if decision.done or decision.action is None:
            success, answer = True, decision.answer
            break
        templated = templatise_action(decision.action, bind_params)
        if failed_out(state, templated):
            log_event("stuck", task_id, steps=len(steps))
            break  # the model insists on a step that keeps failing: end unsuccessful, no loop
        state, snap, _ = execute(state, snap, templated, StepSource.MODEL, None)

    if not success and not stop_reason and len(steps) >= max_steps:
        stop_reason = "step_limit"
    if success and last_event is not None:
        routes.report(last_event.model_copy(update={"goal_reached": True, "goal_text": task.goal_text}))
    log_event("finish", task_id, success=success, steps=len(steps), model_calls=model_calls, replans=replans)
    return RunResult(
        success=success,
        model_calls=model_calls,
        model_tokens=tokens,
        replans=replans,
        answer=answer[:200],
        wall_ms=ms_since(started),
        steps=tuple(steps),
        stop_reason=stop_reason,
    )
