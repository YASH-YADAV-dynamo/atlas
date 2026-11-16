"""GraphStore on HydraDB over Bolt. Cypher lives in `queries.py`; this module only runs it.

HydraDB rejects explicit transactions, so every call is an auto-commit `session.run` with one
statement. Writes are idempotent MERGE/SET (ADR-0001); counters are derived from Event nodes.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import neo4j
from neo4j.exceptions import AuthError, DriverError, ServiceUnavailable, SessionExpired, TransientError

from atlas.adapters.hydradb import queries as q
from atlas.adapters.hydradb.paths import RawPath, parse_path_rows, plain_path_rows
from atlas.adapters.hydradb.retry import call_with_retry
from atlas.contracts.errors import PathFormatError, StoreUnavailableError
from atlas.contracts.ports import STATES_BY_URL_LIMIT
from atlas.contracts.types import (
    Action,
    ChangeEvent,
    Entity,
    Memo,
    Goal,
    MapView,
    Observation,
    RunStep,
    Skill,
    SleepLogEntry,
    SleepLogKind,
    State,
    StoredPath,
    Transition,
)
from atlas.core.cost import edge_cost
from atlas.core.ids import event_node_id

Row = Mapping[str, Any]
_Hop = tuple[tuple[Transition, ...], float, int | None]  # steps, hop cost, skill id

# `native_path_max_len rejected by admission control: actual 23 exceeds limit 16`.
ENGINE_MAX_PATH_EDGES = 16
ENGINE_MAX_STEPS = (ENGINE_MAX_PATH_EDGES + 1) // 2  # n steps are 2n - 1 edges
_MAX_STATS_ROUNDS = 32  # bound on re-deriving one transition's stats under contention


def _state(row: Row) -> State:
    return State(
        id=row["id"], site=row["site"], sig=row["sig"], url_tmpl=row["url_tmpl"], title=row["title"] or ""
    )


def _transition(row: Row) -> Transition:
    last_ok = row["last_ok_at"]
    return Transition(
        id=row["id"],
        site=row["site"],
        from_state=row["src"],
        to_state=row["dst"],
        action=Action.model_validate_json(row["action"]),
        ok=int(row["ok"] or 0),
        fail=int(row["fail"] or 0),
        ms=float(row["ms"] or 0.0),
        cost=float(row["cost"] if row["cost"] is not None else 0.0),
        last_ok_at=datetime.fromisoformat(last_ok) if last_ok else None,
    )


def _known(value: Any) -> int | None:
    """Change ids are stored as 0 for "not known" (the engine has no null SET)."""
    return int(value) if value else None


def _change(row: Row) -> ChangeEvent:
    return ChangeEvent(
        id=row["id"],
        site=row["site"],
        transition_id=row["tid"],
        from_state=row["src"],
        expected_state=row["expected"],
        action=Action.model_validate_json(row["action"]),
        first_seen=datetime.fromisoformat(row["first_seen"]),
        actual_state=_known(row["actual"]),
        replacement_id=_known(row["replacement"]),
    )


class HydraGraphStore:
    def __init__(self, driver: neo4j.Driver, graph: str) -> None:
        self._driver = driver
        self._graph = graph

    def close(self) -> None:
        self._driver.close()

    def verify(self) -> None:
        """Raise StoreUnavailableError unless the engine accepts the token and the graph answers
        a read. The engine runs only MATCH ... RETURN rows (a bare `RETURN 1` is rejected), so the
        probe is a state lookup for an id in no namespace; it matches nothing."""
        try:
            self._driver.verify_connectivity()
        except (DriverError, TransientError, AuthError, OSError) as exc:
            raise StoreUnavailableError(f"HydraDB request failed: {type(exc).__name__}") from exc
        self._run(q.GET_STATE, id=0, ns="")

    def _run(self, statement: str, **params: object) -> list[Any]:
        """One auto-commit statement; returns driver records (still holding native graph values).

        Transient failures are retried (every statement is idempotent, ADR-0001); auth and other
        driver errors are not. Query errors from the engine propagate unchanged.
        """
        try:
            return call_with_retry(lambda: self._run_once(statement, params))
        except (AuthError, DriverError) as exc:
            raise StoreUnavailableError(f"HydraDB request failed: {type(exc).__name__}") from exc

    def _run_once(self, statement: str, params: dict[str, object]) -> list[Any]:
        try:
            with self._driver.session(database=self._graph) as session:
                return list(session.run(statement, params))  # type: ignore[arg-type]
        except (ServiceUnavailable, SessionExpired, TransientError, OSError) as exc:
            raise StoreUnavailableError(f"HydraDB request failed: {type(exc).__name__}") from exc

    def _one(self, statement: str, **params: object) -> Row | None:
        records = self._run(statement, **params)
        return records[0].data() if records else None

    # -- states ---------------------------------------------------------------------------

    def get_state(self, ns: str, state_id: int) -> State | None:
        row = self._one(q.GET_STATE, id=state_id, ns=ns)
        return _state(row) if row else None

    def upsert_state(self, ns: str, state: State) -> bool:
        stored = self.get_state(ns, state.id)
        known = stored is not None
        if stored == state:
            return True  # already stored with the same facts: nothing to write
        row = {
            "id": state.id,
            "ns": ns,
            "site": state.site,
            "sig": state.sig,
            "url_tmpl": state.url_tmpl,
            "title": state.title,
        }
        self._run(q.UPSERT_STATES, rows=[row])
        return known

    def set_state_tokens(self, ns: str, state_id: int, tok: str) -> None:
        at = datetime.now(UTC).isoformat()
        self._run(q.SET_STATE_TOKENS, id=state_id, tok=tok, at=at)

    def states_by_url(self, ns: str, site: str, url_tmpl: str) -> tuple[tuple[int, str], ...]:
        rows = [r.data() for r in self._run(q.STATES_BY_URL, ns=ns, site=site, url_tmpl=url_tmpl)]
        rows.sort(key=lambda row: (row["at"] or "", -int(row["id"])), reverse=True)
        return tuple((int(row["id"]), row["tok"] or "") for row in rows[:STATES_BY_URL_LIMIT])

    # -- evidence -------------------------------------------------------------------------

    def has_event(self, ns: str, event_id: str) -> bool:
        return self._event_recorded(event_node_id(ns, event_id))

    def _event_recorded(self, eid: int) -> bool:
        row = self._one(q.EVENT_EXISTS, eid=eid)
        return bool(row and row["total"])

    def _load_transition(self, ns: str, tid: int) -> Transition | None:
        row = self._one(q.GET_TRANSITION, tid=tid, ns=ns)
        return _transition(row) if row else None

    def record_observation(self, ns: str, obs: Observation) -> Transition:
        tid, src, dst = obs.transition_id, obs.from_state, obs.to_state
        # The id is derived from (ns, from, action, to), so a readable transition already has
        # these edges and facts; its facts are written last, so it is readable only when complete.
        if self._load_transition(ns, tid) is None:
            self._run(q.MERGE_TRANSITION_IN, from_state=src, tid=tid)
            self._run(q.MERGE_TRANSITION_OUT, tid=tid, to_state=dst)
            self._run(q.SET_OUT_EDGE_COST, tid=tid, to_state=dst)
            self._run(
                q.SET_TRANSITION_FACTS,
                tid=tid,
                ns=ns,
                site=obs.site,
                from_state=src,
                to_state=dst,
                action_json=obs.action.model_dump_json(),
            )

        eid = event_node_id(ns, obs.event_id)
        duplicate = self._event_recorded(eid)
        if not duplicate:
            self._run(q.MERGE_EVENT, tid=tid, eid=eid)
            self._run(
                q.SET_EVENT_FACTS,
                eid=eid,
                ns=ns,
                ok=1 if obs.ok else 0,
                fail=0 if obs.ok else 1,
                ms=obs.ms if obs.ok else 0,
                at=obs.at.isoformat(),
            )
            self._run(q.SET_EVENT_RUN, eid=eid, site=obs.site, tid=tid, run=obs.run_id or "")

        # Totals and cost are recomputed even for a duplicate: it repairs a crash between the
        # event write and the stats write, and writes the same values otherwise.
        self._derive_stats(tid, src)
        if obs.ok and not duplicate:
            self._run(q.SET_TRANSITION_LAST_OK, tid=tid, at=obs.at.isoformat())

        loaded = self._load_transition(ns, tid)
        if loaded is None:
            raise StoreUnavailableError("transition not readable after write")
        return loaded

    def _totals(self, tid: int) -> tuple[int, int, float]:
        row = self._one(q.TRANSITION_TOTALS, tid=tid) or {}
        ok, fail = int(row.get("ok") or 0), int(row.get("fail") or 0)
        return ok, fail, float(row.get("ms_total") or 0) / ok if ok else 0.0

    def _derive_stats(self, tid: int, src: int) -> None:
        """Write ok/fail/ms/cost derived from the Event nodes, re-deriving until stable.

        The totals read and the stats write are separate auto-commit statements, so a concurrent
        reporter can overwrite fresh stats with an older snapshot (last writer wins). Each writer
        therefore re-reads the totals after its write and writes again until they match: the
        last stats write is always followed by a check that saw every event, so it is exact.
        """
        written: tuple[int, int, float] | None = None
        for _ in range(_MAX_STATS_ROUNDS):
            totals = self._totals(tid)
            if totals == written:
                return
            ok, fail, ms = totals
            cost = edge_cost(ok, fail, ms)
            self._run(q.SET_TRANSITION_STATS, tid=tid, ok=ok, fail=fail, ms=ms, cost=cost)
            self._run(q.SET_IN_EDGE_COST, from_state=src, tid=tid, cost=cost)
            written = totals
        # Still moving after the bound: events keep arriving, and their writers re-derive.

    # -- goals ----------------------------------------------------------------------------

    def link_goal(self, ns: str, goal: Goal, transition_id: int) -> None:
        self._run(q.MERGE_GOAL_LINK, gid=goal.id, tid=transition_id)
        self._run(q.SET_GOAL_FACTS, gid=goal.id, ns=ns, site=goal.site, text=goal.text)

    def goal_targets(self, ns: str, goal_id: int) -> tuple[int, ...]:
        rows = self._run(q.GOAL_TARGETS, gid=goal_id, ns=ns)
        return tuple(int(rec.data()["id"]) for rec in rows)

    def list_goals(self, ns: str, site: str) -> tuple[Goal, ...]:
        rows = self._run(q.LIST_GOALS, ns=ns, site=site)
        return tuple(Goal.model_validate(rec.data()) for rec in rows)

    # -- context: transitions and entities ------------------------------------------------

    def get_transition(self, ns: str, transition_id: int) -> Transition | None:
        return self._load_transition(ns, transition_id)

    def record_entity(self, ns: str, entity: Entity) -> None:
        eid, tid = entity.id, entity.transition_id
        self._run(q.MERGE_ENTITY_SEEN, eid=eid, tid=tid)
        self._run(
            q.SET_ENTITY_FACTS,
            eid=eid,
            ns=ns,
            site=entity.site,
            type=entity.type,
            key=entity.key,
            tid=tid,
            at=entity.seen_at.isoformat(),
        )

    def get_entity(self, ns: str, entity_id: int) -> Entity | None:
        row = self._one(q.GET_ENTITY, eid=entity_id, ns=ns)
        if row is None:
            return None
        return Entity(
            id=row["id"],
            site=row["site"],
            type=row["type"],
            key=row["key"],
            transition_id=row["last_seen"],
            seen_at=datetime.fromisoformat(row["seen_at"]),
        )

    # -- answer memos ---------------------------------------------------------------------

    def put_memo(self, ns: str, memo: Memo) -> None:
        row = {
            "id": memo.id,
            "ns": ns,
            "site": memo.site,
            "key": memo.key,
            "url": memo.url,
            "title": memo.title,
            "at": memo.at.isoformat(),
        }
        self._run(q.PUT_MEMO, rows=[row])

    def get_memo(self, ns: str, memo_id: int) -> Memo | None:
        row = self._one(q.GET_MEMO, id=memo_id, ns=ns)
        if row is None:
            return None
        return Memo(
            id=row["id"],
            site=row["site"],
            key=row["key"],
            url=row["url"],
            title=row["title"] or "",
            at=datetime.fromisoformat(row["at"]),
        )

    # -- planning -------------------------------------------------------------------------

    def shortest_paths(
        self, ns: str, source: int, target: int, max_steps: int, path_count: int
    ) -> tuple[StoredPath, ...]:
        q.safe_int(path_count, 1, q.MAX_PATH_COUNT)
        # The engine refuses maxLen above ENGINE_MAX_PATH_EDGES (admission control), so longer
        # bounds are searched at the longest route the engine accepts.
        max_steps = min(q.safe_int(max_steps, 1, q.MAX_PATH_EDGES // 2), ENGINE_MAX_STEPS)
        # SPpaths does not return paths cheapest-first (it looks hop-ordered), so a cheap but
        # long path can fall outside the engine's first `path_count`: ask for the maximum,
        # order by summed cost here and keep the best `path_count`.
        statement = q.sppaths(max_steps, q.MAX_PATH_COUNT)  # validates the inlined integers
        if self.get_state(ns, source) is None:
            return ()
        records = self._run(statement, source=source, target=target)
        raw = parse_path_rows(plain_path_rows(records))
        cache: dict[int, Transition | None] = {}
        skills: dict[int, Skill | None] = {}
        found: list[StoredPath] = []
        for path in raw:
            hops = self._hops(ns, path, source, target, cache, skills)
            if hops:
                found.append(
                    StoredPath(
                        steps=tuple(step for hop in hops for step in hop[0]),
                        cost=sum(hop[1] for hop in hops),
                        skill_ids=tuple(hop[2] for hop in hops if hop[2] is not None),
                    )
                )
        found.sort(key=lambda p: (p.cost, [step.id for step in p.steps]))
        return tuple(found[:path_count])

    def _hops(
        self,
        ns: str,
        path: RawPath,
        source: int,
        target: int,
        cache: dict[int, Transition | None],
        skills: dict[int, Skill | None],
    ) -> list[_Hop]:
        """One (steps, hop cost, skill id) per hop; empty if a hop is foreign to `ns`. Every
        second node is an action or a skill; a skill expands into its `via` transitions."""
        ids = path.node_ids
        if len(ids) % 2 != 0 or ids[0] != source or ids[-1] != target:
            raise PathFormatError("path does not run from the source state to the target action")
        hops: list[_Hop] = []
        for node in ids[1::2]:
            step = self._cached_transition(ns, node, cache)
            if step is not None:
                hops.append(((step,), step.cost, None))
                continue
            if node not in skills:
                skills[node] = get_skill(self, ns, node)
            skill = skills[node]
            if skill is None:
                return []  # not a transition or skill of this namespace: never part of its routes
            steps = [self._cached_transition(ns, tid, cache) for tid in skill.via]
            known = [t for t in steps if t is not None]
            if len(known) != len(steps):
                return []
            hops.append((tuple(known), skill.cost, skill.id))
        return hops

    def _cached_transition(self, ns: str, tid: int, cache: dict[int, Transition | None]) -> Transition | None:
        if tid not in cache:
            cache[tid] = self._load_transition(ns, tid)
        return cache[tid]

    # -- changes (ChangeStore) ------------------------------------------------------------

    def recent_outcomes(self, ns: str, transition_id: int, limit: int) -> tuple[bool, ...]:
        statement = q.recent_outcomes(limit)  # validates the inlined limit
        if self._load_transition(ns, transition_id) is None:
            return ()  # Event nodes carry no site: scope by the namespaced transition first
        return tuple(bool(rec.data()["ok"]) for rec in self._run(statement, tid=transition_id))

    def record_change(self, ns: str, change: ChangeEvent) -> None:
        row = self._one(q.GET_CHANGE, cid=change.id, ns=ns)
        existing = _change(row) if row else None
        first_seen = existing.first_seen if existing else change.first_seen
        actual = change.actual_state or (existing.actual_state if existing else None)
        replacement = change.replacement_id or (existing.replacement_id if existing else None)
        self._run(q.MERGE_CHANGE, cid=change.id, tid=change.transition_id)
        self._run(
            q.SET_CHANGE_FACTS,
            cid=change.id,
            ns=ns,
            site=change.site,
            tid=change.transition_id,
            from_state=change.from_state,
            expected_state=change.expected_state,
            action_json=change.action.model_dump_json(),
            first_seen=first_seen.isoformat(),
            actual_state=actual or 0,
            replacement_id=replacement or 0,
        )

    def list_changes(self, ns: str, site: str) -> tuple[ChangeEvent, ...]:
        found = [_change(r.data()) for r in self._run(q.LIST_CHANGES, ns=ns, site=site)]
        return tuple(sorted(found, key=lambda c: (c.first_seen, c.id), reverse=True))

    # -- map ------------------------------------------------------------------------------

    def get_map(self, ns: str, site: str) -> MapView:
        states = tuple(_state(r.data()) for r in self._run(q.MAP_STATES, ns=ns, site=site))
        transitions = tuple(_transition(r.data()) for r in self._run(q.MAP_TRANSITIONS, ns=ns, site=site))
        return MapView(site=site, states=states, transitions=transitions)

    def list_sites(self, ns: str) -> tuple[tuple[str, int, int], ...]:
        return list_sites(self, ns)

    # -- skills (SkillStore, ADR-0006): see the module functions below ----------------------

    def run_steps(self, ns: str, site: str) -> tuple[RunStep, ...]:
        return run_steps(self, ns, site)

    def upsert_skill(self, ns: str, skill: Skill) -> None:
        upsert_skill(self, ns, skill)

    def delete_skill(self, ns: str, skill_id: int) -> None:
        delete_skill(self, ns, skill_id)

    def list_skills(self, ns: str, site: str) -> tuple[Skill, ...]:
        return list_skills(self, ns, site)

    def append_log(self, ns: str, entry: SleepLogEntry) -> None:
        append_log(self, ns, entry)

    def list_log(self, ns: str, site: str) -> tuple[SleepLogEntry, ...]:
        return list_log(self, ns, site)


def list_sites(store: HydraGraphStore, ns: str) -> tuple[tuple[str, int, int], ...]:
    """(site, states, transitions) per mapped site, ordered by site; a site with no transition counts 0."""
    states = {r.data()["site"]: int(r.data()["total"]) for r in store._run(q.SITE_STATE_COUNTS, ns=ns)}  # noqa: SLF001
    edges = {r.data()["site"]: int(r.data()["total"]) for r in store._run(q.SITE_TRANSITION_COUNTS, ns=ns)}  # noqa: SLF001
    return tuple((site, states[site], edges.get(site, 0)) for site in sorted(states))


def _skill(row: Row) -> Skill:
    return Skill(
        id=row["id"],
        site=row["site"],
        from_state=row["src"],
        to_state=row["dst"],
        via=tuple(json.loads(row["via"])),
        runs=int(row["runs"]),
        cost=float(row["cost"]),
    )


def run_steps(store: HydraGraphStore, ns: str, site: str) -> tuple[RunStep, ...]:
    """Steps with a run id, ordered by (run_id, at); a failure sorts before a success recorded
    at the same instant, the order the learner writes them."""
    rows = (r.data() for r in store._run(q.RUN_STEPS, ns=ns, site=site))  # noqa: SLF001
    steps = [
        RunStep(run_id=r["run"], transition_id=r["tid"], ok=bool(r["ok"]), at=datetime.fromisoformat(r["at"]))
        for r in rows
        if r["run"]
    ]
    return tuple(sorted(steps, key=lambda s: (s.run_id, s.at, s.ok)))


def upsert_skill(store: HydraGraphStore, ns: str, skill: Skill) -> None:
    kid, src, dst = skill.id, skill.from_state, skill.to_state
    run = store._run  # noqa: SLF001
    run(q.MERGE_SKILL_IN, from_state=src, kid=kid)
    run(q.MERGE_SKILL_OUT, kid=kid, to_state=dst)
    via_json = json.dumps(list(skill.via))
    run(
        q.SET_SKILL_FACTS,
        kid=kid,
        ns=ns,
        site=skill.site,
        from_state=src,
        to_state=dst,
        via_json=via_json,
        runs=skill.runs,
        cost=skill.cost,
    )
    run(q.SET_SKILL_IN_COST, from_state=src, kid=kid, cost=skill.cost)
    run(q.SET_SKILL_OUT_COST, kid=kid, to_state=dst)


def get_skill(store: HydraGraphStore, ns: str, skill_id: int) -> Skill | None:
    row = store._one(q.GET_SKILL, kid=skill_id, ns=ns)  # noqa: SLF001
    return _skill(row) if row else None


def delete_skill(store: HydraGraphStore, ns: str, skill_id: int) -> None:
    store._run(q.DELETE_SKILL, kid=skill_id, ns=ns)  # noqa: SLF001


def list_skills(store: HydraGraphStore, ns: str, site: str) -> tuple[Skill, ...]:
    found = [_skill(r.data()) for r in store._run(q.LIST_SKILLS, ns=ns, site=site)]  # noqa: SLF001
    return tuple(sorted(found, key=lambda k: k.id))


def append_log(store: HydraGraphStore, ns: str, entry: SleepLogEntry) -> None:
    row = {
        "id": entry.id,
        "ns": ns,
        "site": entry.site,
        "kind": entry.kind.value,
        "skill": entry.skill_id,
        "via": json.dumps(list(entry.via)),
        "evidence": entry.evidence,
        "at": entry.at.isoformat(),
    }
    store._run(q.UPSERT_LOG, rows=[row])  # noqa: SLF001


def _log_entry(row: Row) -> SleepLogEntry:
    return SleepLogEntry(
        id=row["id"],
        site=row["site"],
        kind=SleepLogKind(row["kind"]),
        skill_id=row["skill"],
        via=tuple(json.loads(row["via"])),
        evidence=row["evidence"],
        at=datetime.fromisoformat(row["at"]),
    )


def list_log(store: HydraGraphStore, ns: str, site: str) -> tuple[SleepLogEntry, ...]:
    found = [_log_entry(r.data()) for r in store._run(q.LIST_LOG, ns=ns, site=site)]  # noqa: SLF001
    return tuple(sorted(found, key=lambda e: (e.at, e.id)))


def open_store(url: str, token: str, graph: str) -> HydraGraphStore:
    """Connect over Bolt: user `neo4j`, the bearer token as password, the graph id as database."""
    try:
        driver = neo4j.GraphDatabase.driver(url, auth=("neo4j", token))
    except (DriverError, OSError, ValueError) as exc:
        raise StoreUnavailableError(f"cannot open HydraDB driver: {type(exc).__name__}") from exc
    return HydraGraphStore(driver, graph)
