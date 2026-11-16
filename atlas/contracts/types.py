"""Domain types shared by core, adapters, the Route API, the runner and the UI."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

# Node ids are stable 53-bit hashes (ADR-0002): valid HydraDB vertex ids, exact in JSON and JS.
MAX_ID = 2**53 - 1

# HydraDB rejects path queries longer than 16 edges; a route step is two edges and the path
# ends on an action node, so one query can return at most 8 steps (ADR-0005).
MAX_ROUTE_STEPS = 8

StateId = Annotated[int, Field(ge=1, le=MAX_ID)]
TransitionId = Annotated[int, Field(ge=1, le=MAX_ID)]
GoalId = Annotated[int, Field(ge=1, le=MAX_ID)]

Site = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9.-]{0,62}$")]
Namespace = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,62}$")]
Slot = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")]
EventId = Annotated[str, Field(pattern=r"^[A-Za-z0-9_.:-]{8,80}$")]
ShortText = Annotated[str, Field(max_length=200)]
GoalText = Annotated[str, Field(min_length=1, max_length=300)]
Params = dict[Slot, ShortText]


class Frozen(BaseModel):
    """Base for all contract models: immutable, unknown fields rejected."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class ActionKind(StrEnum):
    CLICK = "click"
    FILL = "fill"
    SELECT = "select"
    CHECK = "check"
    PRESS = "press"
    GOTO = "goto"


class Element(Frozen):
    """One interactive element from the accessibility tree.

    `in_list` marks elements inside repeated data (table rows, list items outside
    navigation). Their names are data, so the signature ignores them.
    """

    role: Annotated[str, Field(min_length=1, max_length=40)]
    name: ShortText = ""
    in_list: bool = False


class Snapshot(Frozen):
    """The accessibility-tree summary a caller already has for the current page."""

    url: Annotated[str, Field(min_length=1, max_length=2048)]
    title: ShortText = ""
    elements: Annotated[tuple[Element, ...], Field(max_length=400)] = ()
    text: Annotated[str, Field(max_length=4000)] = ""  # visible text for the model; never in sig


class Locator(Frozen):
    """Role + accessible-name locator. `name` and `row` may contain `{slot}` placeholders.

    `row` narrows the match to the table row or list item containing that text, for
    lists whose rows repeat one control name (for example every row has "View order").
    """

    role: Annotated[str, Field(min_length=1, max_length=40)]
    name: ShortText
    row: ShortText | None = None


class Fill(Frozen):
    """A form field set before the action's trigger. Exactly one of `param` / `value`."""

    locator: Locator
    kind: Literal[ActionKind.FILL, ActionKind.SELECT, ActionKind.CHECK] = ActionKind.FILL
    param: Slot | None = None
    value: ShortText | None = None

    @model_validator(mode="after")
    def _one_source(self) -> Self:
        if (self.param is None) == (self.value is None):
            raise ValueError("a fill needs exactly one of param or value")
        return self


class Action(Frozen):
    """One state-changing step: optional form `fills`, then one trigger (ADR-0003).

    `param` names the task slot that supplies the trigger's value (fill/select/press);
    `value` is a literal instead (a goto path, a fixed option, a key name).
    """

    kind: ActionKind
    locator: Locator | None = None
    param: Slot | None = None
    value: ShortText | None = None
    fills: Annotated[tuple[Fill, ...], Field(max_length=20)] = ()

    @model_validator(mode="after")
    def _shape(self) -> Self:
        if self.kind is ActionKind.GOTO:
            if self.locator is not None or self.value is None:
                raise ValueError("goto takes a value (path) and no locator")
        elif self.locator is None:
            raise ValueError(f"{self.kind.value} needs a locator")
        if self.param is not None and self.value is not None:
            raise ValueError("an action takes param or value, not both")
        return self


class State(Frozen):
    """An abstract page state: every concrete page with the same `sig` is this state."""

    id: StateId
    site: Site
    sig: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    url_tmpl: Annotated[str, Field(max_length=2048)]
    title: ShortText = ""


class Transition(Frozen):
    """An observed `from_state --action--> to_state` with its learned statistics.

    Stored as an action node between two ACT edges (ADR-0003). `ms` is the mean
    duration of successful runs; `cost` follows `atlas.core.cost.edge_cost`.
    """

    id: TransitionId
    site: Site
    from_state: StateId
    to_state: StateId
    action: Action
    ok: Annotated[int, Field(ge=0)] = 0
    fail: Annotated[int, Field(ge=0)] = 0
    ms: Annotated[float, Field(ge=0)] = 0.0
    cost: Annotated[float, Field(ge=0)]
    last_ok_at: datetime | None = None


class Observation(Frozen):
    """One idempotent piece of evidence for a transition, keyed by `event_id` (ADR-0001)."""

    event_id: EventId
    transition_id: TransitionId
    site: Site
    from_state: StateId
    to_state: StateId
    action: Action
    ok: bool
    ms: Annotated[int, Field(ge=0, le=600_000)]
    at: datetime
    run_id: EventId | None = None  # the run this step belonged to, when the caller said


class StoredPath(Frozen):
    """A path returned by the store: transitions in driving order and their summed cost."""

    steps: Annotated[tuple[Transition, ...], Field(min_length=1)]
    cost: Annotated[float, Field(ge=0)]
    skill_ids: tuple[int, ...] = ()  # skills this path rode; their steps are already expanded


class Goal(Frozen):
    """A goal template (for example `refund order {order_id}`) known to the map."""

    id: GoalId
    site: Site
    text: GoalText


class Entity(Frozen):
    """A task entity already resolved (SPEC 3): for example order 1042, last seen at the
    state reached by `transition_id`, the latest transition whose action used that value."""

    id: Annotated[int, Field(ge=1, le=MAX_ID)]
    site: Site
    type: Slot
    key: ShortText
    transition_id: TransitionId
    seen_at: datetime


class ChangeEvent(Frozen):
    """A detected flow change (SPEC 5.5): a transition with a strong record stopped working.

    `actual_state` / `replacement_id` are set when the same action was seen to lead somewhere
    else instead (the replacement road); they stay None for a control that simply fails.
    """

    id: Annotated[int, Field(ge=1, le=MAX_ID)]
    site: Site
    transition_id: TransitionId
    from_state: StateId
    expected_state: StateId
    action: Action
    first_seen: datetime
    actual_state: StateId | None = None
    replacement_id: TransitionId | None = None


class RunStep(Frozen):
    """One recorded step of a run, as the sleep phase reads it back."""

    run_id: EventId
    transition_id: TransitionId
    ok: bool
    at: datetime


class Skill(Frozen):
    """A proven multi-step path compressed into one hop (SPEC 5.1, ADR-0006).

    Stored like a transition, `(from)-[:ACT {cost}]->(:Skill)-[:ACT {cost: 0}]->(to)`, so one
    path query crosses it in two edges however many steps `via` holds.
    """

    id: Annotated[int, Field(ge=1, le=MAX_ID)]
    site: Site
    from_state: StateId
    to_state: StateId
    via: Annotated[tuple[TransitionId, ...], Field(min_length=3)]
    runs: Annotated[int, Field(ge=3)]  # distinct runs that drove the whole path successfully
    cost: Annotated[float, Field(ge=0)]


class SleepLogKind(StrEnum):
    SKILL_CREATED = "skill_created"
    SKILL_REMOVED = "skill_removed"


class SleepLogEntry(Frozen):
    """One change the sleep phase made, with the evidence for it."""

    id: Annotated[int, Field(ge=1, le=MAX_ID)]
    site: Site
    kind: SleepLogKind
    skill_id: Annotated[int, Field(ge=1, le=MAX_ID)]
    via: tuple[TransitionId, ...]
    evidence: ShortText
    at: datetime


class SleepReport(Frozen):
    site: Site
    created: tuple[Skill, ...] = ()
    removed: tuple[int, ...] = ()


class MapView(Frozen):
    """Everything the UI needs to draw one site's map."""

    site: Site
    states: tuple[State, ...]
    transitions: tuple[Transition, ...]


class LocateResult(Frozen):
    state_id: StateId
    known: bool


class RouteStep(Frozen):
    """One step of a planned route. `action` still carries `{slot}` placeholders."""

    action: Action
    expect_state: StateId
    reliability: Annotated[float, Field(gt=0, lt=1)]
    last_verified: datetime | None = None


class PlanReason(StrEnum):
    ROUTE_FOUND = "route_found"
    UNKNOWN_GOAL = "unknown_goal"
    NO_ROUTE_WITHIN_MAX_STEPS = "no_route_within_max_steps"
    MISSING_PARAMS = "missing_params"


class PlanResult(Frozen):
    """A route, or `needs_model=True` with the reason the map could not supply one.

    `max_steps_used` is the largest step bound the planner actually searched with,
    so an empty route is never ambiguous about how far it looked (R-PLAN-4).
    """

    route: tuple[RouteStep, ...] = ()
    confidence: Annotated[float, Field(ge=0, le=1)] = 0.0
    needs_model: bool
    reason: PlanReason
    max_steps_used: Annotated[int, Field(ge=0)] = 0

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        found = self.reason is PlanReason.ROUTE_FOUND
        if found != bool(self.route) or found == self.needs_model:
            raise ValueError("route, needs_model and reason disagree")
        return self


class ReportEvent(Frozen):
    """What a caller observed after trying one action. Idempotent on `event_id`.

    `ok` means the action executed. `expect_state` is the planned destination when the
    action came from a route; a different actual state counts as a failure of that
    transition. `goal_reached` links the goal to this transition; re-sending the same
    `event_id` with `goal_reached=True` adds the link without recounting (R-LEARN-4).
    """

    site: Site
    from_state: StateId
    action: Action
    to_snapshot: Snapshot
    ok: bool
    ms: Annotated[int, Field(ge=0, le=600_000)]
    event_id: EventId
    expect_state: StateId | None = None
    goal_text: GoalText | None = None
    params: Params = Field(default_factory=dict)
    goal_reached: bool = False
    run_id: EventId | None = None  # same value for every step of one task run (enables skills)

    @model_validator(mode="after")
    def _goal_needs_text(self) -> Self:
        if self.goal_reached and self.goal_text is None:
            raise ValueError("goal_reached needs goal_text")
        return self


class ReportResult(Frozen):
    """Outcome of a report: where the caller is now and which transitions changed."""

    state_id: StateId
    known: bool
    duplicate: bool
    transitions: tuple[Transition, ...] = ()


class StepSource(StrEnum):
    MAP = "map"
    MODEL = "model"


class TaskSpec(Frozen):
    """A task for the reference runner. `start_path` is relative to the site root."""

    site: Site
    goal_text: GoalText
    params: Params = Field(default_factory=dict)
    start_path: Annotated[str, Field(pattern=r"^/[^\s]{0,500}$")] = "/"


class StepLog(Frozen):
    """One executed step, streamed to the UI and kept in the run result."""

    index: Annotated[int, Field(ge=0)]
    source: StepSource
    action: Action
    from_state: StateId
    to_state: StateId | None
    ok: bool
    ms: Annotated[int, Field(ge=0)]


class RunResult(Frozen):
    success: bool
    model_calls: Annotated[int, Field(ge=0)]
    model_tokens: Annotated[int, Field(ge=0)]
    replans: Annotated[int, Field(ge=0)]
    answer: ShortText = ""  # for read-only tasks ("find the email of ...")
    stop_reason: ShortText = ""  # why an unsuccessful run ended: budget_exceeded, explore_limit, step_limit
    wall_ms: Annotated[int, Field(ge=0)]
    steps: tuple[StepLog, ...]


class ModelDecision(Frozen):
    """The exploration model's answer: the next action, or `done` when the goal is met."""

    action: Action | None
    done: bool
    answer: ShortText = ""  # set with `done` when the task asks for a value
    input_tokens: Annotated[int, Field(ge=0)] = 0
    output_tokens: Annotated[int, Field(ge=0)] = 0

    @model_validator(mode="after")
    def _one_outcome(self) -> Self:
        if self.done == (self.action is not None):
            raise ValueError("a decision is either done or one action")
        return self
