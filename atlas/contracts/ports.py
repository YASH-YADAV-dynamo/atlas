"""Ports: the only interfaces core, the API and the runner depend on."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from atlas.contracts.api_models import (
    LocateRequest,
    LocateResponse,
    PlanRequest,
    PlanResponse,
    ReportRequest,
    ReportResponse,
)
from atlas.contracts.types import (
    Action,
    ChangeEvent,
    Entity,
    Goal,
    MapView,
    ModelDecision,
    Observation,
    Params,
    RunStep,
    Skill,
    SleepLogEntry,
    Snapshot,
    State,
    StoredPath,
    Transition,
)

STATES_BY_URL_LIMIT = 50


class GraphStore(Protocol):
    """The map. Every method is scoped to a namespace (`ns`, one per tenant) and every
    write is idempotent. The in-memory fake and the HydraDB adapter pass one contract suite.
    """

    def upsert_state(self, ns: str, state: State) -> bool:
        """Store the state. Returns True when it was already known before this call."""
        ...

    def get_state(self, ns: str, state_id: int) -> State | None: ...

    def set_state_tokens(self, ns: str, state_id: int, tok: str) -> None:
        """Store the state's token fingerprint (`atlas.core.signature.fingerprint`, may be "")
        and mark it as the newest state of its URL template. Idempotent; the state must exist."""
        ...

    def states_by_url(self, ns: str, site: str, url_tmpl: str) -> tuple[tuple[int, str], ...]:
        """(state id, fingerprint) of the site's states with this URL template, newest
        `set_state_tokens` first, at most `STATES_BY_URL_LIMIT`. States never given a
        fingerprint come last with "". Read only."""
        ...

    def has_event(self, ns: str, event_id: str) -> bool:
        """True when an observation with this `event_id` has been recorded in `ns`."""
        ...

    def record_observation(self, ns: str, obs: Observation) -> Transition:
        """Add one piece of evidence and return the transition with fresh totals.

        Creates the transition on first sight. An `event_id` already recorded changes
        nothing. `ok`/`fail` count distinct events, `ms` is the mean over successful
        events, `cost` is `atlas.core.cost.edge_cost(ok, fail, ms)`.
        """
        ...

    def link_goal(self, ns: str, goal: Goal, transition_id: int) -> None:
        """Record that completing `transition_id` achieves `goal`. Idempotent."""
        ...

    def goal_targets(self, ns: str, goal_id: int) -> tuple[int, ...]:
        """Ids of the transitions known to achieve the goal; empty when unknown."""
        ...

    def shortest_paths(
        self, ns: str, source: int, target: int, max_steps: int, path_count: int
    ) -> tuple[StoredPath, ...]:
        """Up to `path_count` simple paths that start at state `source` and end by
        executing transition `target`, each at most `max_steps` hops long,
        cheapest first. Empty when none exists within `max_steps`.

        A skill counts as one hop and costs its own `cost`; the returned `steps` hold
        its transitions expanded in order, and `skill_ids` names the skills used.
        """
        ...

    def get_map(self, ns: str, site: str) -> MapView: ...


class ContextStore(Protocol):
    """Reads and writes the MCP layer needs on top of GraphStore: goals as skills, entities."""

    def list_goals(self, ns: str, site: str) -> tuple[Goal, ...]:
        """Every goal known for the site, ordered by id."""
        ...

    def get_transition(self, ns: str, transition_id: int) -> Transition | None: ...

    def record_entity(self, ns: str, entity: Entity) -> None:
        """Upsert by `entity.id`; the latest sighting replaces the previous one. Idempotent."""
        ...

    def get_entity(self, ns: str, entity_id: int) -> Entity | None: ...


class SiteStore(Protocol):
    """Map sizes, for the MCP resource `atlas://sites` (increment 6)."""

    def list_sites(self, ns: str) -> tuple[tuple[str, int, int], ...]:
        """(site, state count, transition count) for every mapped site, ordered by site."""
        ...


class ChangeStore(Protocol):
    """Outcome history and flow-change events (increment 3)."""

    def recent_outcomes(self, ns: str, transition_id: int, limit: int) -> tuple[bool, ...]:
        """The transition's latest outcomes (True = success), newest first by event time."""
        ...

    def record_change(self, ns: str, change: ChangeEvent) -> None:
        """Upsert by `change.id`. An existing event keeps its `first_seen`; `actual_state` and
        `replacement_id` are filled in when the new record carries them. Idempotent."""
        ...

    def list_changes(self, ns: str, site: str) -> tuple[ChangeEvent, ...]:
        """The site's change events, newest `first_seen` first."""
        ...


class SkillStore(Protocol):
    """Run history, skills and the sleep-phase log (increment 7)."""

    def run_steps(self, ns: str, site: str) -> tuple[RunStep, ...]:
        """Every recorded step that carried a run id, ordered by (run_id, at)."""
        ...

    def upsert_skill(self, ns: str, skill: Skill) -> None:
        """Create or replace the skill node and its two ACT edges. Idempotent."""
        ...

    def delete_skill(self, ns: str, skill_id: int) -> None: ...

    def list_skills(self, ns: str, site: str) -> tuple[Skill, ...]:
        """The site's skills, ordered by id."""
        ...

    def append_log(self, ns: str, entry: SleepLogEntry) -> None:
        """Idempotent on `entry.id`."""
        ...

    def list_log(self, ns: str, site: str) -> tuple[SleepLogEntry, ...]:
        """Oldest first."""
        ...


class RouteStore(GraphStore, ChangeStore, Protocol):
    """What the Route API needs once change detection is on."""


class MapStore(GraphStore, ContextStore, ChangeStore, Protocol):
    """The full store. The in-memory fake and the HydraDB adapter implement every part."""


class BrowserDriver(Protocol):
    """A browser session someone else provides. Atlas only reads it and acts in it."""

    def snapshot(self) -> Snapshot: ...

    def act(self, action: Action) -> None:
        """Run a bound action (no `{slot}` left). Raises `BrowserActionError` on failure."""
        ...

    def url(self) -> str: ...


class ModelClient(Protocol):
    """The exploration model: asked for one step only where the map ends."""

    def next_action(
        self, goal_text: str, params: Params, snapshot: Snapshot, history: tuple[str, ...]
    ) -> ModelDecision: ...


class RouteApi(Protocol):
    """The three Route API calls as the runner sees them."""

    def locate(self, request: LocateRequest) -> LocateResponse: ...

    def plan(self, request: PlanRequest) -> PlanResponse: ...

    def report(self, request: ReportRequest) -> ReportResponse: ...


class Clock(Protocol):
    def now(self) -> datetime:
        """Timezone-aware UTC now."""
        ...
