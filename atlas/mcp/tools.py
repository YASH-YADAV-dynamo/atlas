"""MCP tool behaviour: plain Python on core + MapStore + an optional BrowserDriver (no MCP SDK here).

One `AtlasTools` is one agent session. The session state (goal, offered choices, pending form
fills, last event) lives on the instance; the map lives only in the store.

Run/act state machine (runner mode):

    run  -> snapshot -> plan -> drive mapped steps (bind, act, report with expect_state)
            |                      mismatch: replan from the page actually reached
            |                      map exhausted (needs_model): stop, offer the page's controls
    act  -> a field control only records a fill (no browser, no map change)
         -> a link/button executes with the pending fills as one action, is reported, and the
            same call replans and keeps driving; `goal_reached` links the goal to the last event.
"""

from __future__ import annotations

import re
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal, cast

from pydantic import ValidationError

from atlas.contracts import mcp_models as m
from atlas.contracts.api_models import FinishRequest, FinishStep, PlanRequest, StartRequest
from atlas.contracts.errors import (
    BrowserActionError,
    MissingParamError,
    ToolUnavailableError,
)
from atlas.contracts.ports import BrowserDriver, Clock, MapStore, SessionStore, SiteStore
from atlas.contracts.types import (
    MAX_ROUTE_STEPS,
    Action,
    ActionKind,
    Entity,
    Fill,
    Goal,
    Locator,
    PlanReason,
    ReportEvent,
    ReportResult,
    RouteStep,
    Snapshot,
    Transition,
)
from atlas.core import session as core_session
from atlas.core.blocked import blocked_reason, unlearned_result
from atlas.core.cost import reliability
from atlas.core.goals import content_words, skill_match, visible_goals
from atlas.core.ids import entity_id
from atlas.core.learner import report as learn
from atlas.core.locate import locate, resolve
from atlas.core.params import bind_action, required_slots, templatise_action
from atlas.core.planner import plan as plan_route
from atlas.mcp.budget import enforce_budget
from atlas.mcp.pending import hold_or_fold

_SLOT = re.compile(r"\{([a-z][a-z0-9_]*)\}")
_SPACES = re.compile(r"\s+")
_FILL_KINDS: dict[str, Literal[ActionKind.FILL, ActionKind.SELECT, ActionKind.CHECK]] = {
    "textbox": ActionKind.FILL,
    "searchbox": ActionKind.FILL,
    "combobox": ActionKind.SELECT,
    "listbox": ActionKind.SELECT,
    "checkbox": ActionKind.CHECK,
    "radio": ActionKind.CHECK,
    "switch": ActionKind.CHECK,
}
_MAX_MESSAGE = 150


@dataclass
class _Target:
    """What an offered choice id really points at (full name, never the cut one)."""

    role: str
    name: str
    repeated: bool


@dataclass
class _Frontier:
    state_id: int
    summary: str
    choices: tuple[m.Choice, ...]
    truncated: bool
    targets: dict[int, _Target]


@dataclass
class _Session:
    goal_text: str
    params: dict[str, str]
    frontier: _Frontier | None = None
    fills: dict[tuple[str, str, str | None], Fill] = field(default_factory=dict)
    last_event: ReportEvent | None = None
    run_id: str = field(default_factory=lambda: f"mcp-run-{uuid.uuid4().hex}")  # one per atlas_run


def _failed(message: str) -> m.RunOutput:
    return m.RunOutput(status=m.RunStatus.FAILED, message=message[:_MAX_MESSAGE])


def _summary(snapshot: Snapshot) -> str:
    text = _SPACES.sub(" ", f"{snapshot.title}: {snapshot.text}" if snapshot.text else snapshot.title)
    return text.strip()[: m.SUMMARY_LENGTH]


def _offer(snapshot: Snapshot) -> tuple[tuple[m.Choice, ...], dict[int, _Target], bool]:
    """Controls of a page as choices: repeated list controls collapse into one `repeated` choice,
    at most MAX_CHOICES, names cut for display; the target table keeps the real names."""
    seen: set[tuple[str, str, bool]] = set()
    choices: list[m.Choice] = []
    targets: dict[int, _Target] = {}
    truncated = False
    for element in snapshot.elements:
        key = (element.role, element.name, element.in_list)
        if key in seen:
            continue
        if len(choices) == m.MAX_CHOICES:
            truncated = True
            break
        seen.add(key)
        number = len(choices)
        choices.append(
            m.Choice(
                id=number,
                role=element.role,
                name=element.name[: m.CHOICE_NAME_LENGTH],
                repeated=element.in_list,
            )
        )
        targets[number] = _Target(element.role, element.name, element.in_list)
    return tuple(choices), targets, truncated


class AtlasTools:
    def __init__(
        self,
        store: MapStore,
        clock: Clock,
        ns: str,
        site: str,
        driver: BrowserDriver | None = None,
        *,
        max_steps: int = 25,
        read_only: bool = False,
        new_state_gate: Callable[[], None] | None = None,
        on_new_state: Callable[[], None] | None = None,
    ) -> None:
        # Hosted private keys: `new_state_gate` raises when the map is full, `on_new_state` counts.
        self._new_state_gate = new_state_gate
        self._on_new_state = on_new_state
        self._store = store
        self._clock = clock
        self._ns = ns
        self._site = site
        self._driver = driver
        self._max_steps = max_steps
        self._session: _Session | None = None
        # Read-only (the public demo key): nothing is written, not even a newly seen page.
        self._read_only = read_only

    @property
    def store(self) -> MapStore:
        return self._store

    @property
    def namespace(self) -> str:
        return self._ns

    # ---- atlas_find_skills --------------------------------------------------------------

    def find_skills(self, request: m.FindSkillsInput) -> m.FindSkillsOutput:
        here = (
            locate(self._store, self._ns, request.site, self._driver.snapshot()).state_id
            if self._driver
            else None
        )
        ranked: list[tuple[int, int, int, m.SkillInfo]] = []
        # Content words only (stopwords and retrieval verbs out), at least half of the query's
        # shared, so "give a customer their money back" offers nothing unrelated (SHIP_PLAN A5).
        for goal in visible_goals(self._store.list_goals(self._ns, request.site)):
            overlap = skill_match(request.goal_text, goal.text)
            if overlap == 0:
                continue
            words = content_words(goal.text)
            transitions = self._goal_transitions(goal)
            if overlap == 0 or not transitions:
                continue
            slots = tuple(dict.fromkeys(_SLOT.findall(goal.text)))
            rates = [reliability(t.ok, t.fail) for t in transitions]
            info = m.SkillInfo(
                skill_id=f"goal:{goal.id}",
                name=goal.text,
                params=slots,
                reliability=sum(rates) / len(rates),
            )
            if here is not None:
                info = self._from_here(info, here, transitions)
            ranked.append((-overlap, len(words), goal.id, info))
        if not ranked:
            self._check_site(request.site)
        ranked.sort(key=lambda row: row[:3])
        skills = tuple(row[3] for row in ranked[: m.MAX_SKILLS])
        return enforce_budget(m.FindSkillsOutput(skills=skills, truncated=len(ranked) > m.MAX_SKILLS))

    def _from_here(self, info: m.SkillInfo, here: int, targets: list[Transition]) -> m.SkillInfo:
        """Runner mode: steps of the cheapest route from the current page, and whether it rides a skill."""
        paths = [
            p for t in targets for p in self._store.shortest_paths(self._ns, here, t.id, MAX_ROUTE_STEPS, 1)
        ]
        if not paths:
            return info
        best = min(paths, key=lambda p: p.cost)
        return info.model_copy(update={"est_steps": len(best.steps), "compressed": bool(best.skill_ids)})

    def _goal_transitions(self, goal: Goal) -> list[Transition]:
        found = (
            self._store.get_transition(self._ns, tid) for tid in self._store.goal_targets(self._ns, goal.id)
        )
        return [t for t in found if t is not None]

    # ---- atlas_run ----------------------------------------------------------------------

    def run(self, request: m.RunInput) -> m.RunOutput:
        driver = self._need_driver()
        if request.skill_id is not None:
            goal = self._goal_by_skill(request.skill_id)
            if goal is None:
                return _failed("unknown skill_id; call atlas_find_skills first")
            goal_text = goal.text
            missing = [slot for slot in _SLOT.findall(goal_text) if slot not in request.params]
            if missing:
                return _failed(f"missing parameter: {missing[0]}")
        else:
            assert request.goal_text is not None
            goal_text = request.goal_text
        self._session = _Session(goal_text=goal_text, params=dict(request.params))
        return enforce_budget(self._continue(self._session, driver.snapshot(), 0, []))

    def _goal_by_skill(self, skill_id: str) -> Goal | None:
        kind, _, number = skill_id.partition(":")
        if kind != "goal":
            return None
        return next(
            (g for g in visible_goals(self._store.list_goals(self._ns, self._site)) if str(g.id) == number),
            None,
        )

    # ---- atlas_act ----------------------------------------------------------------------

    def act(self, request: m.ActInput) -> m.RunOutput:
        driver = self._need_driver()
        session = self._session
        if session is None:
            return _failed("no task in progress; call atlas_run first")
        if request.goal_reached:
            return enforce_budget(self._complete(session, driver))
        frontier = session.frontier
        if frontier is None:
            return _failed("no page to act on; call atlas_run to start a task")
        assert request.action is not None
        target = frontier.targets.get(request.action)
        if target is None:
            return _failed(f"unknown action {request.action}; use an id from the last choices")
        if request.row is not None and not target.repeated:
            return _failed("row applies only to repeated controls")
        locator = Locator(role=target.role, name=target.name, row=request.row)
        kind = _FILL_KINDS.get(target.role)
        if kind is not None:
            if request.value is None:
                return _failed(f"this {target.role} needs a value")
            session.fills[(target.role, target.name, request.row)] = Fill(
                locator=locator, kind=kind, value=request.value
            )
            return enforce_budget(
                m.RunOutput(
                    status=m.RunStatus.NEEDS_CHOICE,
                    result_summary=frontier.summary,
                    choices=frontier.choices,
                    truncated=frontier.truncated,
                )
            )
        if request.value is not None:
            return _failed(f"a {target.role} takes no value")
        try:
            action = Action(kind=ActionKind.CLICK, locator=locator, fills=tuple(session.fills.values()))
        except ValidationError:
            return _failed("too many pending form fills")
        entities: list[m.EntityRef] = []
        after = self._execute(action)
        if isinstance(after, m.RunOutput):
            return after
        snapshot, ms = after
        self._report(session, frontier.state_id, action, snapshot, ms, None, entities)
        session.fills.clear()
        session.frontier = None
        return enforce_budget(self._continue(session, snapshot, 0, entities))

    def _complete(self, session: _Session, driver: BrowserDriver) -> m.RunOutput:
        if session.last_event is None:
            return _failed("nothing to complete; take at least one step first")
        learn(
            self._store,
            self._clock,
            self._ns,
            session.last_event.model_copy(update={"goal_reached": True, "goal_text": session.goal_text}),
        )
        self._session = None
        return m.RunOutput(status=m.RunStatus.DONE, result_summary=_summary(driver.snapshot()))

    # ---- the driving loop ---------------------------------------------------------------

    def _continue(
        self, session: _Session, snapshot: Snapshot, driven: int, entities: list[m.EntityRef]
    ) -> m.RunOutput:
        """From the page `snapshot`: follow the planned route, replanning after a mismatch, until
        the map ends (frontier), the goal route completes, or a step fails."""
        failed_steps: set[tuple[int, str]] = set()  # transitions that failed in this call
        while True:
            state_id = self._locate(self._site, snapshot, session.params)
            planned = plan_route(
                self._store,
                self._ns,
                PlanRequest(
                    site=self._site,
                    state_id=state_id,
                    goal_text=session.goal_text,
                    params=session.params,
                    snapshot=snapshot,
                ),
            )
            if planned.needs_model:
                return self._frontier(session, snapshot, state_id, driven, entities)
            if planned.route and _step_key(state_id, planned.route[0].action) in failed_steps:
                return self._frontier(session, snapshot, state_id, driven, entities)
            last = len(planned.route) - 1
            for index, step in enumerate(planned.route):
                if driven >= self._max_steps:
                    return _failed(f"step limit {self._max_steps} reached")
                try:
                    action = bind_action(step.action, session.params)
                except MissingParamError as error:
                    return _failed(str(error))
                try:
                    outcome = self._execute_mapped(action)
                except BrowserActionError:
                    # R-MCP-9: one failure on this transition, then replan from the real page.
                    snapshot = self._driver_snapshot_or_none() or snapshot
                    self._report_failure(session, state_id, action, snapshot, step.expect_state)
                    failed_steps.add(_step_key(state_id, step.action))
                    break
                snapshot, ms = outcome
                driven += 1
                reached = self._report(session, state_id, action, snapshot, ms, step.expect_state, entities)
                state_id = reached.state_id
                if state_id != step.expect_state:
                    break  # the page is not where the map said: replan from here
                if index == last and session.last_event is not None:
                    learn(
                        self._store,
                        self._clock,
                        self._ns,
                        session.last_event.model_copy(update={"goal_reached": True}),
                    )
            else:
                self._session = None
                return m.RunOutput(
                    status=m.RunStatus.DONE,
                    steps_driven=driven,
                    model_calls_saved=driven,
                    result_summary=_summary(snapshot),
                    entities_resolved=_cap_entities(entities),
                    truncated=len(entities) > m.MAX_ENTITIES,
                )

    def _execute(self, action: Action) -> tuple[Snapshot, int] | m.RunOutput:
        """Run one bound action. On failure nothing is reported: browser and map stay as they were."""
        assert self._driver is not None
        started = time.perf_counter()
        try:
            self._driver.act(action)
            snapshot = self._driver.snapshot()
        except BrowserActionError as error:
            return _failed(f"step failed: {error}")
        return snapshot, min(int((time.perf_counter() - started) * 1000), 600_000)

    def _execute_mapped(self, action: Action) -> tuple[Snapshot, int]:
        assert self._driver is not None
        started = time.perf_counter()
        self._driver.act(action)
        snapshot = self._driver.snapshot()
        return snapshot, min(int((time.perf_counter() - started) * 1000), 600_000)

    def _driver_snapshot_or_none(self) -> Snapshot | None:
        assert self._driver is not None
        try:
            return self._driver.snapshot()
        except BrowserActionError:
            return None

    def _report_failure(
        self, session: _Session, from_state: int, action: Action, snapshot: Snapshot, expect_state: int
    ) -> None:
        event = ReportEvent(
            site=self._site,
            from_state=from_state,
            action=action,
            to_snapshot=snapshot,
            ok=False,
            ms=0,
            event_id=f"mcp-{uuid.uuid4().hex}",
            expect_state=expect_state,
            goal_text=session.goal_text,
            params=session.params,
            run_id=session.run_id,
        )
        learn(self._store, self._clock, self._ns, event)

    def _report(
        self,
        session: _Session,
        from_state: int,
        action: Action,
        snapshot: Snapshot,
        ms: int,
        expect_state: int | None,
        entities: list[m.EntityRef],
    ) -> ReportResult:
        event = ReportEvent(
            site=self._site,
            from_state=from_state,
            action=action,
            to_snapshot=snapshot,
            ok=True,
            ms=ms,
            event_id=f"mcp-{uuid.uuid4().hex}",
            expect_state=expect_state,
            goal_text=session.goal_text,
            params=session.params,
            run_id=session.run_id,
        )
        result = learn(self._store, self._clock, self._ns, event)
        session.last_event = event
        if result.transitions:
            self._record_entities(session, action, result.transitions[-1], entities)
        return result

    def _record_entities(
        self, session: _Session, action: Action, transition: Transition, entities: list[m.EntityRef]
    ) -> None:
        slots = required_slots(templatise_action(action, session.params)) & session.params.keys()
        for slot in sorted(slots):
            key = session.params[slot]
            self._store.record_entity(
                self._ns,
                Entity(
                    id=entity_id(self._ns, self._site, slot, key),
                    site=self._site,
                    type=slot,
                    key=key,
                    transition_id=transition.id,
                    seen_at=self._clock.now(),
                ),
            )
            ref = m.EntityRef(type=slot, key=key)
            if ref not in entities:
                entities.append(ref)

    def _frontier(
        self,
        session: _Session,
        snapshot: Snapshot,
        state_id: int,
        driven: int,
        entities: list[m.EntityRef],
    ) -> m.RunOutput:
        choices, targets, capped = _offer(snapshot)
        offered = enforce_budget(
            m.RunOutput(
                status=m.RunStatus.NEEDS_CHOICE,
                steps_driven=driven,
                model_calls_saved=driven,
                result_summary=_summary(snapshot),
                entities_resolved=_cap_entities(entities),
                choices=choices,
                truncated=capped or len(entities) > m.MAX_ENTITIES,
            )
        )
        # Only ids the agent actually received stay valid, so every id maps to a real control.
        kept = {n: targets[n] for n in range(len(offered.choices))}
        session.frontier = _Frontier(
            state_id=state_id,
            summary=offered.result_summary,
            choices=offered.choices,
            truncated=offered.truncated,
            targets=kept,
        )
        return offered

    # ---- atlas_recall -------------------------------------------------------------------

    def recall(self, request: m.RecallInput) -> m.RecallOutput:
        entity = self._store.get_entity(
            self._ns, entity_id(self._ns, self._site, request.entity_type, request.key)
        )
        transition = self._store.get_transition(self._ns, entity.transition_id) if entity else None
        if transition is None:
            return m.RecallOutput(found=False)
        state = self._store.get_state(self._ns, transition.to_state)
        page = state.url_tmpl[:200] if state else ""
        found = m.RecallOutput(found=True, state_id=transition.to_state, page=page)
        if self._driver is None:
            return enforce_budget(found)
        here = locate(self._store, self._ns, self._site, self._driver.snapshot()).state_id
        if here == transition.to_state:
            return enforce_budget(found)
        bind = {request.entity_type: request.key}
        for path in self._store.shortest_paths(self._ns, here, transition.id, m.MAX_BRIEF_STEPS, 3):
            try:
                route = tuple(
                    m.BriefStep(
                        action=bind_action(t.action, bind),
                        expect=t.to_state,
                        reliability=reliability(t.ok, t.fail),
                    )
                    for t in path.steps
                )
            except MissingParamError:
                continue
            return enforce_budget(found.model_copy(update={"route": route}))
        return enforce_budget(found.model_copy(update={"needs_model": True}))

    # ---- planner mode: atlas_plan / atlas_report ----------------------------------------

    def plan(self, request: m.PlanToolInput) -> m.PlanToolOutput:
        snapshot = request.snapshot_summary
        seen = None
        if blocked_reason(snapshot) is None and (self._read_only or self._new_state_gate is not None):
            seen = resolve(self._store, self._ns, request.site, snapshot, request.params)
            if self._read_only and self._store.get_state(self._ns, seen.state_id) is None:
                self._check_site(request.site)
                # A page this map has never seen, and a read-only key may not record it.
                return m.PlanToolOutput(
                    state_id=seen.state_id,
                    needs_model=True,
                    reason=PlanReason.NO_ROUTE_WITHIN_MAX_STEPS,
                )
            if not self._read_only and not seen.known and self._new_state_gate is not None:
                self._new_state_gate()

        def fits(script: str) -> bool:
            probe = m.PlanToolOutput(
                state_id=2**53 - 1,
                plan_id="0" * 12,
                script=script,
                script_steps=1,
                needs_model=False,
                reason=PlanReason.ROUTE_FOUND,
                match="similar",
                goal="x" * 80,
            )
            return m.response_tokens(probe) <= m.MCP_RESPONSE_TOKEN_BUDGET

        started = core_session.start(
            cast(SessionStore, self._store),
            self._ns,
            StartRequest(
                site=request.site,
                snapshot=snapshot,
                goal_text=request.goal_text,
                params=request.params,
                replay=request.replay,
            ),
            read_only=self._read_only,
            final_step_varies=self._final_step_varies,
            script_fits=fits,
        )
        if seen is not None and not seen.known and not self._read_only and self._on_new_state is not None:
            self._on_new_state()
        steps = tuple(
            m.BriefStep(action=s.action, expect=s.expect_state, reliability=s.reliability)
            for s in started.route
        )
        return enforce_budget(
            m.PlanToolOutput(
                state_id=started.state_id,
                plan_id=started.plan_id,
                route=steps[: m.MAX_BRIEF_STEPS],
                open_url=started.open_url,
                script=started.script,
                script_steps=started.script_steps,
                search_url=started.search_url,
                needs_model=started.needs_model,
                reason=started.reason,
                truncated=len(steps) > m.MAX_BRIEF_STEPS,
                blocked=started.blocked,
                match=started.match,
                goal=started.goal,
                params=started.params,
            )
        )

    def _final_step_varies(self, targets: tuple[int, ...], route: tuple[RouteStep, ...]) -> bool:
        """True when the last step is a literal pick that differed between successful runs."""
        if required_slots(route[-1].action):
            return False  # bound from the task's parameters, so it already varies correctly
        actions = {
            t.action.model_dump_json()
            for t in (self._store.get_transition(self._ns, tid) for tid in targets)
            if t is not None and not required_slots(t.action)
        }
        return len(actions) > 1

    def report(self, request: m.ReportToolInput | m.ReportInput) -> m.ReportToolOutput:
        if self._read_only:
            raise ToolUnavailableError("this key is read-only: atlas_report is disabled")
        if isinstance(request, m.ReportInput):
            if request.action is None:
                return self._finish(request)
            request = request.as_single()
        event_id = request.event_id or f"mcp-{uuid.uuid4().hex}"
        event = ReportEvent(
            site=request.site,
            from_state=request.from_state,
            action=request.action,
            to_snapshot=request.to_snapshot_summary,
            ok=request.ok,
            ms=request.ms,
            event_id=event_id,
            expect_state=request.expect_state,
            goal_text=request.goal_text,
            params=request.params,
            goal_reached=request.goal_reached,
            run_id=request.run_id,
            kind=request.kind,
        )
        folded = event if blocked_reason(event.to_snapshot) else hold_or_fold(self._ns, event)
        if folded is None:
            # A same-page form fill: held and folded into the action that submits it, so a
            # replayed route fills the field first (a self-loop would never be planned).
            here = request.from_state
            known = self._store.get_state(self._ns, here) is not None
            return m.ReportToolOutput(state_id=here, known=known, duplicate=False, event_id=event_id)
        result = learn(self._store, self._clock, self._ns, folded)
        if request.ok and result.transitions and not result.duplicate and result.blocked is None:
            self._record_entities(
                _Session(goal_text=request.goal_text or "", params=dict(request.params)),
                folded.action,
                result.transitions[-1],
                [],
            )
        return m.ReportToolOutput(
            state_id=result.state_id,
            known=result.known,
            duplicate=result.duplicate,
            event_id=event_id,
            blocked=result.blocked,
        )

    def _finish(self, request: m.ReportInput) -> m.ReportToolOutput:
        """A batch (explored steps, fills folded inside this one call) or a replay outcome
        (`plan_id`, `done`): the same core as REST `/v1/finish`."""
        event_id = request.event_id or f"mcp-{uuid.uuid4().hex}"
        finished = core_session.finish(
            cast(SessionStore, self._store),
            self._clock,
            self._ns,
            FinishRequest(
                site=request.site,
                plan_id=request.plan_id,
                done=request.done,
                from_state=request.from_state,
                steps=tuple(
                    FinishStep(action=step.action, to=step.to, ok=step.ok, ms=step.ms)
                    for step in request.steps
                ),
                to_snapshot=request.to_snapshot_summary,
                ok=request.ok,
                ms=request.ms,
                goal_text=request.goal_text,
                params=request.params,
                goal_reached=request.goal_reached,
                kind=request.kind,
                event_id=event_id,
            ),
        )
        return m.ReportToolOutput(
            state_id=finished.state_id,
            known=True,
            duplicate=finished.learned == 0,
            event_id=event_id,
            blocked=finished.blocked,
            learned=finished.learned,
            goal=finished.goal,
        )

    # ---- helpers ------------------------------------------------------------------------

    def _locate(self, site: str, snapshot: Snapshot, params: Mapping[str, str] | None = None) -> int:
        blocked = blocked_reason(snapshot)
        if blocked is not None:
            return unlearned_result(self._store, self._ns, site, snapshot, params, blocked).state_id
        if self._read_only:
            return resolve(self._store, self._ns, site, snapshot, params).state_id
        return locate(self._store, self._ns, site, snapshot, params).state_id

    def _check_site(self, site: str) -> None:
        """A read-only key cannot map a new site, so naming an unmapped one is an error that says
        which sites exist; a writable key starts every site empty and simply gets no result."""
        if not self._read_only:
            return
        sites = cast(SiteStore, self._store)  # every MapStore adapter also serves site listings
        known = [name for name, _, _ in sites.list_sites(self._ns)]
        if site not in known:
            listed = ", ".join(known[:10]) or "none"
            raise ToolUnavailableError(
                f"unknown site {site!r}; mapped sites: {listed}. This key is read-only: to map a "
                "new site, create a private key with POST /v1/keys"
            )

    def _need_driver(self) -> BrowserDriver:
        if self._driver is None:
            raise ToolUnavailableError("this server has no browser; use atlas_plan and atlas_report")
        return self._driver


def _step_key(state_id: int, action: Action) -> tuple[int, str]:
    return state_id, action.model_dump_json()


def _cap_entities(entities: Sequence[m.EntityRef]) -> tuple[m.EntityRef, ...]:
    return tuple(entities[: m.MAX_ENTITIES])
