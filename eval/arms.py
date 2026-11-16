"""Benchmark arms B0 (plain model-driven agent), B1 (trajectory cache) and A (Atlas via the Route API).

Every arm navigates to the task's start page, counts model calls and provider-reported tokens through a
proxy, and sets `success = ctx.check(answer)`: the arm's own belief is never the result. Same model and
same step cap for every arm.
"""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from atlas.contracts.bench_models import Arm, BenchTask, Split, TaskRecord
from atlas.contracts.errors import BrowserActionError, MissingParamError, ModelUnavailableError
from atlas.contracts.ports import BrowserDriver, ModelClient, RouteApi
from atlas.contracts.types import Action, ActionKind, ModelDecision, Params, Snapshot
from atlas.core.params import bind_action, templatise_action
from atlas.runner.run import describe, run_task

HISTORY_LINES = 6


def note_model_error(arm: Arm, task: BenchTask, exc: ModelUnavailableError) -> None:
    """A failed model call ends the task unsuccessfully (the DB check still decides); the run goes on."""
    sys.stderr.write(f"eval: model error arm={arm.value} template={task.template_id}: {exc}\n")


class TrajectoryCache:
    """B1's memory: the successful action list per task template (templated actions, plain dict)."""

    def __init__(self) -> None:
        self._scripts: dict[str, tuple[Action, ...]] = {}

    def get(self, template_id: str) -> tuple[Action, ...] | None:
        return self._scripts.get(template_id)

    def put(self, template_id: str, actions: Sequence[Action]) -> None:
        self._scripts[template_id] = tuple(actions)

    def copy(self) -> TrajectoryCache:
        """A snapshot for held-out evaluation, where nothing may be learned."""
        clone = TrajectoryCache()
        clone._scripts = dict(self._scripts)
        return clone

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {key: [a.model_dump(mode="json") for a in value] for key, value in self._scripts.items()}
        path.write_text(json.dumps(data, indent=1), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> TrajectoryCache:
        cache = cls()
        for key, value in json.loads(path.read_text(encoding="utf-8")).items():
            cache.put(key, [Action.model_validate(item) for item in value])
        return cache


@dataclass(frozen=True)
class ArmContext:
    run_id: str
    seed: int
    iteration: int
    split: Split
    driver: BrowserDriver
    model: ModelClient
    check: Callable[[str], bool]
    routes: RouteApi | None = None
    cache: TrajectoryCache | None = None
    secrets: Mapping[str, str] | None = None
    max_steps: int = 25


class CountingModel:
    """Counts calls and the provider's reported tokens of the model it wraps."""

    def __init__(self, inner: ModelClient) -> None:
        self._inner = inner
        self.calls = 0
        self.input_tokens = 0
        self.output_tokens = 0

    def next_action(
        self, goal_text: str, params: Params, snapshot: Snapshot, history: tuple[str, ...]
    ) -> ModelDecision:
        decision = self._inner.next_action(goal_text, params, snapshot, history)
        self.calls += 1
        self.input_tokens += decision.input_tokens
        self.output_tokens += decision.output_tokens
        return decision


def _record(
    task: BenchTask,
    ctx: ArmContext,
    arm: Arm,
    model: CountingModel,
    *,
    answer: str,
    actions: int,
    started: float,
    replans: int = 0,
) -> TaskRecord:
    return TaskRecord(
        run_id=ctx.run_id,
        arm=arm,
        seed=ctx.seed,
        iteration=ctx.iteration,
        split=ctx.split,
        template_id=task.template_id,
        success=ctx.check(answer),
        model_calls=model.calls,
        input_tokens=model.input_tokens,
        output_tokens=model.output_tokens,
        actions=actions,
        replans=replans,
        wall_ms=max(0, int((time.monotonic() - started) * 1000)),
    )


class _Session:
    """One task on one browser: executes templated actions, remembers what worked."""

    def __init__(self, task: BenchTask, ctx: ArmContext) -> None:
        self.task, self.ctx = task, ctx
        self.bind = {**task.spec.params, **(ctx.secrets or {})}
        self.wire = {**task.spec.params, **{slot: f"{{{slot}}}" for slot in ctx.secrets or {}}}
        self.steps = 0  # executed actions, failed ones included
        self.worked: list[Action] = []
        self.history: list[str] = []
        ctx.driver.act(Action(kind=ActionKind.GOTO, value=task.spec.start_path))

    def execute(self, templated: Action) -> bool:
        self.steps += 1
        try:
            self.ctx.driver.act(bind_action(templated, self.bind))
        except (BrowserActionError, MissingParamError):
            self.history.append(f"FAILED: {describe(templated)}")
            return False
        self.worked.append(templated)
        self.history.append(f"ok: {describe(templated)}")
        return True

    def ask_model(self, model: CountingModel, arm: Arm) -> str:
        """The plain agent loop: one model call per step until it says done. Returns its answer."""
        while self.steps < self.ctx.max_steps:
            try:
                decision = model.next_action(
                    self.task.spec.goal_text,
                    self.wire,
                    self.ctx.driver.snapshot(),
                    tuple(self.history[-HISTORY_LINES:]),
                )
            except ModelUnavailableError as exc:
                note_model_error(arm, self.task, exc)
                return ""
            if decision.done or decision.action is None:
                return decision.answer
            self.execute(templatise_action(decision.action, self.bind))
        return ""


def run_b0(task: BenchTask, ctx: ArmContext) -> TaskRecord:
    started = time.monotonic()
    model = CountingModel(ctx.model)
    session = _Session(task, ctx)
    answer = session.ask_model(model, Arm.B0)
    return _record(task, ctx, Arm.B0, model, answer=answer, actions=session.steps, started=started)


def run_b1(task: BenchTask, ctx: ArmContext) -> TaskRecord:
    started = time.monotonic()
    cache = ctx.cache if ctx.cache is not None else TrajectoryCache()
    model = CountingModel(ctx.model)
    session = _Session(task, ctx)
    script = cache.get(task.template_id)
    replayed = script is not None and all(session.execute(step) for step in script)
    # a miss, a broken script, or a task that must read a value goes to the model
    answer = session.ask_model(model, Arm.B1) if not replayed or task.needs_answer else ""
    record = _record(task, ctx, Arm.B1, model, answer=answer, actions=session.steps, started=started)
    if record.success and not replayed and session.worked:
        cache.put(task.template_id, session.worked)
    return record


def run_atlas(task: BenchTask, ctx: ArmContext) -> TaskRecord:
    if ctx.routes is None:
        raise ValueError("the Atlas arm needs a RouteApi in ctx.routes")
    model = CountingModel(ctx.model)
    started = time.monotonic()
    try:
        result = run_task(
            task.spec, ctx.routes, ctx.driver, model, secrets=ctx.secrets, max_steps=ctx.max_steps
        )
    except ModelUnavailableError as exc:
        note_model_error(Arm.ATLAS, task, exc)
        return _record(task, ctx, Arm.ATLAS, model, answer="", actions=0, started=started)
    return TaskRecord(
        run_id=ctx.run_id,
        arm=Arm.ATLAS,
        seed=ctx.seed,
        iteration=ctx.iteration,
        split=ctx.split,
        template_id=task.template_id,
        success=ctx.check(result.answer),
        model_calls=model.calls,
        input_tokens=model.input_tokens,
        output_tokens=model.output_tokens,
        actions=len(result.steps),
        replans=result.replans,
        wall_ms=result.wall_ms,
    )
