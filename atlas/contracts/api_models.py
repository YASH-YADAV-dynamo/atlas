"""Route API request and response models (SPEC 3b). Shared by HTTP and MCP."""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from atlas.contracts.types import (
    Action,
    ChangeEvent,
    EventId,
    Frozen,
    GoalKind,
    GoalMatchKind,
    GoalText,
    LocateResult,
    Params,
    PlanReason,
    PlanResult,
    ReportEvent,
    RouteStep,
    Site,
    Skill,
    SleepLogEntry,
    StateId,
)
from atlas.core.snapshot import LenientSnapshot

LOCATE_PATH = "/v1/locate"
PLAN_PATH = "/v1/plan"
REPORT_PATH = "/v1/report"
START_PATH = "/v1/start"
FINISH_PATH = "/v1/finish"
MAP_PATH = "/v1/map/{site}"
CHANGES_PATH = "/v1/changes"  # GET ?site=
SLEEP_PATH = "/v1/sleep"  # POST ?site= : run the sleep phase now
SKILLS_PATH = "/v1/skills"  # GET ?site=
HEALTH_PATH = "/healthz"


class LocateRequest(Frozen):
    site: Site
    snapshot: LenientSnapshot
    params: Params = Field(default_factory=dict)  # the task's values: a page showing one is an entity page


class LocateResponse(LocateResult):
    pass


class PlanRequest(Frozen):
    site: Site
    state_id: StateId
    goal_text: GoalText
    params: Params = Field(default_factory=dict)
    # The current page. When it lists elements, a route is returned only if its first step's
    # control is on it (guards against a page recognised by similarity, or a merged list page).
    snapshot: LenientSnapshot | None = None


class PlanResponse(PlanResult):
    blocked: str | None = None  # the snapshot is a blocked or error page: no route, nothing learned


class ReportRequest(ReportEvent):
    to_snapshot: LenientSnapshot


class ReportResponse(Frozen):
    state_id: StateId
    known: bool
    duplicate: bool
    blocked: str | None = None  # a blocked or error page: not learned


PlanId = Annotated[str, Field(pattern=r"^[A-Za-z0-9]{12}$")]
MAX_FINISH_STEPS = 12


class StartRequest(Frozen):
    """`POST /v1/start`: locate, match the goal and plan in one round trip (SHIP_PLAN 3.4)."""

    site: Site
    snapshot: LenientSnapshot
    goal_text: GoalText
    params: Params = Field(default_factory=dict)
    replay: Literal["steps", "script"] = "steps"  # "script": one Playwright function replaces `route`


class StartResponse(Frozen):
    """Where the caller is and how to reach the goal. `route`, `open_url` or neither (explore).

    `reason` is `route_found` whenever a route or an `open_url` is returned. `open_url` is for
    read goals: navigate there instead of running the route. `search_url` (with `{q}`) is the
    site's search entry, offered on an unknown goal. `plan_id` names the issued route or URL:
    send it back to `/v1/finish`. `params` are values read out of the goal text.
    """

    state_id: StateId
    known: bool
    matched: Literal["similar", "url"] | None = None
    plan_id: PlanId | None = None
    match: GoalMatchKind | None = None
    goal: GoalText | None = None
    params: Params | None = None
    route: tuple[RouteStep, ...] = ()
    confidence: Annotated[float, Field(ge=0, le=1)] = 0.0
    open_url: Annotated[str, Field(max_length=2048)] | None = None
    search_url: Annotated[str, Field(max_length=2048)] | None = None
    script: Annotated[str, Field(max_length=900)] | None = None  # `replay: "script"`: replaces `route`
    script_steps: Annotated[int, Field(ge=1)] | None = None  # route steps the script covers
    needs_model: bool
    reason: PlanReason
    max_steps_used: Annotated[int, Field(ge=0)] = 0
    blocked: str | None = None  # the snapshot is a blocked or error page: nothing learned


class FinishStep(Frozen):
    """One action of an exploration, with the page it led to."""

    action: Action
    to: LenientSnapshot
    ok: bool = True
    ms: Annotated[int, Field(ge=0, le=600_000)] = 0


class FinishRequest(Frozen):
    """`POST /v1/finish`: the outcome of a task, in one call (SHIP_PLAN 3.4).

    Any of: a replay outcome (`plan_id`, with `done` steps credited to the issued route; when
    `ok` is false the next step is charged one failure), explored `steps` from `from_state`
    (at most 12, in order), and the final page `to_snapshot`. `goal_reached` links the goal to
    the last action; a `read` goal that was reached also remembers the final page's address.
    """

    site: Site
    plan_id: PlanId | None = None
    done: Annotated[int, Field(ge=0, le=64)] | None = None
    from_state: StateId | None = None
    steps: Annotated[tuple[FinishStep, ...], Field(max_length=MAX_FINISH_STEPS)] = ()
    to_snapshot: LenientSnapshot | None = None
    ok: bool
    ms: Annotated[int, Field(ge=0, le=600_000)] = 0  # total time of the replayed steps
    goal_text: GoalText | None = None
    params: Params = Field(default_factory=dict)
    goal_reached: bool = False
    kind: GoalKind = "act"
    event_id: EventId | None = None  # makes a retried call idempotent; generated when absent

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.plan_id is None and self.steps == () and self.to_snapshot is None:
            raise ValueError("finish needs plan_id, steps or to_snapshot")
        if self.done is not None and self.plan_id is None:
            raise ValueError("done needs plan_id")
        if self.steps and self.from_state is None:
            raise ValueError("steps need from_state")
        if self.goal_reached and self.goal_text is None:
            raise ValueError("goal_reached needs goal_text")
        return self


class FinishResponse(Frozen):
    """`state_id`: the page the caller ended on. `learned`: steps stored. `goal`: the goal key."""

    state_id: StateId
    learned: Annotated[int, Field(ge=0)]
    goal: GoalText | None = None
    blocked: str | None = None  # the final page is a blocked or error page: not learned


class ChangesResponse(Frozen):
    """`GET /v1/changes?site=`: detected flow changes, newest first."""

    site: Site
    changes: tuple[ChangeEvent, ...] = ()


class SkillsResponse(Frozen):
    """`GET /v1/skills?site=`: the skills and the sleep-phase change log (oldest first)."""

    site: Site
    skills: tuple[Skill, ...] = ()
    log: tuple[SleepLogEntry, ...] = ()


class ErrorBody(Frozen):
    """The only error envelope the API returns for an `AtlasError`."""

    code: str
    message: str
