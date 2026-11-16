"""Route API request and response models (SPEC 3b). Shared by HTTP and MCP."""

from __future__ import annotations

from pydantic import Field

from atlas.contracts.types import (
    ChangeEvent,
    Frozen,
    GoalText,
    LocateResult,
    Params,
    PlanResult,
    ReportEvent,
    Site,
    Skill,
    SleepLogEntry,
    StateId,
)
from atlas.core.snapshot import LenientSnapshot

LOCATE_PATH = "/v1/locate"
PLAN_PATH = "/v1/plan"
REPORT_PATH = "/v1/report"
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
