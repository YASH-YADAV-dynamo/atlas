"""MCP tool contract (SPEC 3c): names, descriptions, input and output models, token budget.

The server registers exactly `TOOLS`; schemas come from these models, and the planner-mode
tools reuse the Route API types, so HTTP and MCP cannot drift.

Token budget. Every tool result is the text `tool_text(result)` and must satisfy
`response_tokens(result) <= MCP_RESPONSE_TOKEN_BUDGET`. Tokens are counted as
ceil(characters / 3) of that text: deterministic, needs no tokenizer, and deliberately
stricter than the usual rule of thumb of about four characters per token. The benchmark
(arm M) reports the provider's real usage numbers next to this estimate.
"""

from __future__ import annotations

import json
import math
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal, Self, cast

from pydantic import BaseModel, Field, model_validator

from atlas.contracts.api_models import PlanId
from atlas.contracts.types import (
    Action,
    EventId,
    Frozen,
    GoalKind,
    GoalMatchKind,
    GoalText,
    Params,
    PlanReason,
    ShortText,
    Site,
    Slot,
    StateId,
)
from atlas.core.snapshot import LenientSnapshot

MCP_RESPONSE_TOKEN_BUDGET = 400
MCP_DEFINITIONS_TOKEN_BUDGET = {"runner": 1000, "planner": 2500}  # all tool definitions of a mode
CHARS_PER_TOKEN = 3
MAX_SKILLS = 5
MAX_CHOICES = 20
MAX_ENTITIES = 5
MAX_BRIEF_STEPS = 12
CHOICE_NAME_LENGTH = 40
SUMMARY_LENGTH = 200

Summary = Annotated[str, Field(max_length=SUMMARY_LENGTH)]
MAX_NOTES = 3
NOTE_LENGTH = 80
Notes = Annotated[tuple[Annotated[str, Field(max_length=NOTE_LENGTH)], ...], Field(max_length=MAX_NOTES)]


def tool_text(result: BaseModel) -> str:
    """The exact text a tool returns: compact JSON without nulls and defaults."""
    return result.model_dump_json(exclude_none=True, exclude_defaults=True)


def count_tokens(text: str) -> int:
    return math.ceil(len(text) / CHARS_PER_TOKEN)


def response_tokens(result: BaseModel) -> int:
    return count_tokens(tool_text(result))


class Mode(StrEnum):
    RUNNER = "runner"  # the server owns a browser session through BrowserDriver
    PLANNER = "planner"  # the agent drives its own browser; Atlas plans and learns


class RunStatus(StrEnum):
    DONE = "done"
    NEEDS_CHOICE = "needs_choice"
    FAILED = "failed"


# ---- atlas_find_skills ------------------------------------------------------------------


class FindSkillsInput(Frozen):
    # No site: every site of the key (at most MAX_SITES). No goal_text: the most reliable goals.
    site: Site | None = None
    goal_text: GoalText | None = None


class SkillInfo(Frozen):
    """A goal (or, later, a SKILL edge) the map can already drive. `skill_id` feeds atlas_run."""

    skill_id: Annotated[str, Field(pattern=r"^(goal|skill):[0-9]{1,16}$")]
    name: GoalText
    params: tuple[Slot, ...] = ()
    reliability: Annotated[float, Field(gt=0, lt=1)]
    est_steps: Annotated[int, Field(ge=1)] | None = None  # from the current page, when known
    compressed: bool = False  # its route from the current page rides a compressed skill
    site: Site | None = None  # set when the search covered several sites


class FindSkillsOutput(Frozen):
    skills: Annotated[tuple[SkillInfo, ...], Field(max_length=MAX_SKILLS)] = ()
    truncated: bool = False
    notes: Notes = ()  # corrections the server made to the input


# ---- atlas_run / atlas_act --------------------------------------------------------------


class RunInput(Frozen):
    """Exactly one of `goal_text` / `skill_id`."""

    goal_text: GoalText | None = None
    skill_id: Annotated[str, Field(pattern=r"^(goal|skill):[0-9]{1,16}$")] | None = None
    params: Params = Field(default_factory=dict)

    @model_validator(mode="after")
    def _one_target(self) -> Self:
        if (self.goal_text is None) == (self.skill_id is None):
            raise ValueError("give exactly one of goal_text or skill_id")
        return self


class ActInput(Frozen):
    """One exploratory step on the page last returned as `needs_choice`.

    `action` is the `id` of an offered choice. A textbox, combobox or checkbox takes `value`
    and is remembered as a form fill; a link or button is the trigger that executes the
    step together with the fills given since the last trigger. `row` picks the list row
    containing that text when the choice is `repeated`. `goal_reached=True` (without
    `action`) declares the goal complete after the last step.
    """

    action: Annotated[int, Field(ge=0, lt=MAX_CHOICES)] | None = None
    value: ShortText | None = None
    row: ShortText | None = None
    goal_reached: bool = False

    @model_validator(mode="after")
    def _one_intent(self) -> Self:
        if self.goal_reached == (self.action is not None):
            raise ValueError("give an action, or goal_reached=true alone")
        if self.goal_reached and (self.value is not None or self.row is not None):
            raise ValueError("goal_reached takes no value or row")
        return self


class Choice(Frozen):
    """An interactive element on the frontier page. `repeated`: one per list row; pass `row`."""

    id: Annotated[int, Field(ge=0, lt=MAX_CHOICES)]
    role: Annotated[str, Field(min_length=1, max_length=40)]
    name: Annotated[str, Field(max_length=CHOICE_NAME_LENGTH)]
    repeated: bool = False


class EntityRef(Frozen):
    type: Slot
    key: ShortText


class RunOutput(Frozen):
    """Result of atlas_run and atlas_act.

    `steps_driven` counts steps executed from the map in this call; `model_calls_saved`
    is the number of model round trips those steps would have cost a snapshot-per-step
    agent (one per step). `choices` is present only with `needs_choice`.
    """

    status: RunStatus
    steps_driven: Annotated[int, Field(ge=0)] = 0
    model_calls_saved: Annotated[int, Field(ge=0)] = 0
    result_summary: Summary = ""
    entities_resolved: Annotated[tuple[EntityRef, ...], Field(max_length=MAX_ENTITIES)] = ()
    choices: Annotated[tuple[Choice, ...], Field(max_length=MAX_CHOICES)] = ()
    message: Summary = ""  # short and actionable, set when status is failed
    truncated: bool = False

    @model_validator(mode="after")
    def _choices_only_at_the_frontier(self) -> Self:
        if self.choices and self.status is not RunStatus.NEEDS_CHOICE:
            raise ValueError("choices are only returned with needs_choice")
        return self


# ---- atlas_recall -----------------------------------------------------------------------


class RecallInput(Frozen):
    entity_type: Slot
    key: ShortText


class BriefStep(Frozen):
    """A route step for an agent that drives its own browser. The action is bound (no slots)
    in atlas_recall, and carries `{slot}` placeholders in atlas_plan."""

    action: Action
    expect: StateId
    reliability: Annotated[float, Field(gt=0, lt=1)]


class RecallOutput(Frozen):
    """Where an entity was last seen and, when the current page is known, the route there."""

    found: bool
    state_id: StateId | None = None
    page: Annotated[str, Field(max_length=200)] = ""  # the state's URL template
    route: Annotated[tuple[BriefStep, ...], Field(max_length=MAX_BRIEF_STEPS)] = ()
    needs_model: bool = False  # found, but no known route from the current page
    truncated: bool = False


# ---- atlas_plan / atlas_report (planner mode) -------------------------------------------


class PlanToolInput(Frozen):
    site: Site
    snapshot_summary: LenientSnapshot
    goal_text: GoalText
    params: Params = Field(default_factory=dict)
    replay: Literal["steps", "script"] = "steps"  # "script": one Playwright function replaces route
    kind: GoalKind | None = None  # accepted for compatibility; "act" switches reworded matches off


class PlanToolOutput(Frozen):
    state_id: StateId
    plan_id: PlanId | None = None  # name of the issued route, script or URL: send it to atlas_report
    route: Annotated[tuple[BriefStep, ...], Field(max_length=MAX_BRIEF_STEPS)] = ()
    open_url: Annotated[str, Field(max_length=2048)] | None = None  # read goals: go here, skip the route
    script: Annotated[str, Field(max_length=900)] | None = None  # replay="script": replaces route
    script_steps: Annotated[int, Field(ge=1)] | None = None
    search_url: Annotated[str, Field(max_length=2048)] | None = None  # this site's search, with {q}
    needs_model: bool
    reason: PlanReason
    truncated: bool = False
    blocked: str | None = None  # a blocked or error page: not learned, ask the model
    match: GoalMatchKind | None = None  # how the goal resolved (exact, same_words, similar)
    goal: GoalText | None = None  # the stored goal when the wording differed: reuse it
    params: Params | None = None  # values read out of the goal text
    notes: Notes = ()  # corrections the server made to the input


class ReportToolInput(Frozen):
    """The Route API report with the bookkeeping fields optional (the server fills them)."""

    site: Site
    from_state: StateId
    action: Action
    to_snapshot_summary: LenientSnapshot
    ok: bool
    ms: Annotated[int, Field(ge=0, le=600_000)] = 0
    event_id: EventId | None = None
    expect_state: StateId | None = None
    goal_text: GoalText | None = None
    params: Params = Field(default_factory=dict)
    goal_reached: bool = False
    run_id: EventId | None = None
    kind: GoalKind = "act"  # "read": a lookup, nothing on the site changed


MAX_REPORT_STEPS = 12


class ReportStep(Frozen):
    """One executed step of a batch; form fills go in the action that submits."""

    action: Action
    to: LenientSnapshot
    ok: bool = True
    ms: Annotated[int, Field(ge=0, le=600_000)] = 0


class ReportInput(Frozen):
    """One of: action (one step), steps (a batch), plan_id (replay)."""

    site: Site
    ok: bool | None = None  # default: a batch is ok when all steps are, a replay when done >= route
    from_state: StateId | None = None
    action: Action | None = None
    to_snapshot_summary: LenientSnapshot | None = None
    steps: Annotated[tuple[ReportStep, ...], Field(max_length=MAX_REPORT_STEPS)] = ()
    plan_id: PlanId | None = None
    done: Annotated[int, Field(ge=0, le=64)] | None = None
    ms: Annotated[int, Field(ge=0, le=600_000)] = 0
    event_id: EventId | None = None
    expect_state: StateId | None = None
    goal_text: GoalText | None = None
    params: Params = Field(default_factory=dict)
    goal_reached: bool = False
    run_id: EventId | None = None
    kind: GoalKind = "act"  # "read": reaching the page was the goal, nothing on the site changed

    @model_validator(mode="after")
    def _one_form(self) -> Self:
        single = self.action is not None
        batch = bool(self.steps)
        replay = self.plan_id is not None
        if single + batch + replay != 1:
            raise ValueError("give exactly one of: action, steps, plan_id")
        if single and (self.from_state is None or self.to_snapshot_summary is None):
            raise ValueError("a single step needs from_state, action and to_snapshot_summary")
        if batch and (self.from_state is None or self.to_snapshot_summary is not None):
            raise ValueError("a batch needs from_state and steps (the last step's page is the final one)")
        if self.done is not None and not replay:
            raise ValueError("done needs plan_id")
        if self.goal_reached and self.goal_text is None:
            raise ValueError("goal_reached needs goal_text")
        return self

    def as_single(self) -> ReportToolInput:
        """The legacy single-step view (only valid for the single form)."""
        assert self.action is not None and self.from_state is not None
        assert self.to_snapshot_summary is not None
        return ReportToolInput(
            site=self.site,
            from_state=self.from_state,
            action=self.action,
            to_snapshot_summary=self.to_snapshot_summary,
            ok=True if self.ok is None else self.ok,
            ms=self.ms,
            event_id=self.event_id,
            expect_state=self.expect_state,
            goal_text=self.goal_text,
            params=self.params,
            goal_reached=self.goal_reached,
            run_id=self.run_id,
            kind=self.kind,
        )


class ReportToolOutput(Frozen):
    state_id: StateId
    known: bool
    duplicate: bool
    event_id: EventId  # echo, so a retry can reuse it
    blocked: str | None = None  # a blocked or error page: not learned
    learned: Annotated[int, Field(ge=0)] | None = None  # steps stored (batch and replay forms)
    goal: GoalText | None = None  # the goal key stored
    notes: Notes = ()  # corrections the server made to the input


# ---- resources (read-only context a client can attach) ----------------------------------

SITES_URI = "atlas://sites"
SITE_CHANGES_URI = "atlas://site/{site}/changes"
MAX_SITES = 20
MAX_CHANGES = 10


class SiteInfo(Frozen):
    site: Site
    states: Annotated[int, Field(ge=0)]
    transitions: Annotated[int, Field(ge=0)]


class SitesResource(Frozen):
    """`atlas://sites`: the mapped sites and their sizes, ordered by site name."""

    sites: Annotated[tuple[SiteInfo, ...], Field(max_length=MAX_SITES)] = ()
    truncated: bool = False


class ChangeBrief(Frozen):
    """One flow change in a sentence-sized shape: which control, from where, since when."""

    control: Annotated[str, Field(max_length=CHOICE_NAME_LENGTH)]  # the action's control name
    page: Annotated[str, Field(max_length=200)]  # URL template of the page the action starts on
    first_seen: datetime
    replaced: bool  # a replacement road is known


class ChangesResource(Frozen):
    """`atlas://site/{site}/changes`: recent flow changes, newest first."""

    site: Site
    changes: Annotated[tuple[ChangeBrief, ...], Field(max_length=MAX_CHANGES)] = ()
    truncated: bool = False


# ---- registry ---------------------------------------------------------------------------


class ToolSpec(Frozen):
    name: Annotated[str, Field(pattern=r"^atlas_[a-z_]{3,30}$")]
    description: Annotated[str, Field(min_length=20, max_length=240)]
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    modes: frozenset[Mode]


BOTH = frozenset({Mode.RUNNER, Mode.PLANNER})

TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="atlas_find_skills",
        description="List up to 5 things the map already knows how to do on this site that match the goal. "
        "Call first; pass a returned skill_id to atlas_run.",
        input_model=FindSkillsInput,
        output_model=FindSkillsOutput,
        modes=BOTH,
    ),
    ToolSpec(
        name="atlas_run",
        description="Do a task in the browser Atlas controls. Drives every known step in one call. "
        "Returns done, or needs_choice with the page's controls where the map ends.",
        input_model=RunInput,
        output_model=RunOutput,
        modes=frozenset({Mode.RUNNER}),
    ),
    ToolSpec(
        name="atlas_act",
        description="After needs_choice: use one offered control (value for inputs, row for repeated list "
        "controls), or set goal_reached when the task is complete. Atlas learns the step and keeps driving.",
        input_model=ActInput,
        output_model=RunOutput,
        modes=frozenset({Mode.RUNNER}),
    ),
    ToolSpec(
        name="atlas_recall",
        description="Look up something already resolved (for example entity_type order_id, key 1042): "
        "the page where it was found and the route to it.",
        input_model=RecallInput,
        output_model=RecallOutput,
        modes=BOTH,
    ),
    ToolSpec(
        name="atlas_plan",
        description="For agents with their own browser: given the current page summary and a goal, "
        "return the known route as steps to execute, or needs_model where the map ends.",
        input_model=PlanToolInput,
        output_model=PlanToolOutput,
        modes=frozenset({Mode.PLANNER}),
    ),
    ToolSpec(
        name="atlas_report",
        description="For agents with their own browser: report what you executed (one step, a batch "
        "of steps, or a plan_id replay outcome) so the map learns it. Safe to retry with the same event_id.",
        input_model=ReportInput,
        output_model=ReportToolOutput,
        modes=frozenset({Mode.PLANNER}),
    ),
)


def tools_for(mode: Mode) -> tuple[ToolSpec, ...]:
    return tuple(tool for tool in TOOLS if mode in tool.modes)


def _untitled(node: object) -> object:
    """The schema without pydantic's generated `title` strings (clients do not need them)."""
    if isinstance(node, dict):
        items = cast(dict[str, object], node)
        return {k: _untitled(v) for k, v in items.items() if not (k == "title" and isinstance(v, str))}
    if isinstance(node, list):
        return [_untitled(v) for v in cast(list[object], node)]
    return node


def definition_tokens(tool: ToolSpec) -> int:
    """Context cost of one tool definition: name, description and input schema (no titles)."""
    schema = json.dumps(_untitled(tool.input_model.model_json_schema()), separators=(",", ":"))
    return count_tokens(tool.name + tool.description + schema)
