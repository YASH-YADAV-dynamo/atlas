"""Every Cypher statement Atlas sends to HydraDB (Release 0 set). Nothing else may hold Cypher.

Engine rules these statements respect (docs/architecture.md engine facts, ADR-0003):
- one statement per request; no transactions, so every write is an idempotent MERGE or SET;
- MERGE matches on id only: MERGE by id, then SET properties in a separate MATCH ... SET;
- an edgeless node can only be written with the UNWIND ... MERGE ... SET batch form (Bolt);
- MATCH is directed with exactly one relationship type; WHERE has no IN / CONTAINS;
- path procedures take scalar parameters only, and `maxLen` counts edges.

Tenancy: one HydraDB graph, one namespace per tenant. The namespace is hashed into every
node id (`atlas.core.ids`), so edges never cross tenants and path queries cannot leave the
caller's namespace; scans additionally filter on the stored `ns` property.

All user-supplied values travel as `$parameters`. The only values ever inlined are the two
integers of `sppaths`, and only after `safe_int` has validated them.
"""

from __future__ import annotations

from atlas.contracts.errors import UnsafeQueryValueError

REL_ACT = "ACT"
MAX_PATH_EDGES = 16  # engine admission limit (max_traversal_hops), see ADR-0005
MAX_PATH_COUNT = 20

UPSERT_STATES = (
    "UNWIND $rows AS row MERGE (n {id: row.id}) "
    "SET n:State, n.ns = row.ns, n.site = row.site, n.sig = row.sig, "
    "n.url_tmpl = row.url_tmpl, n.title = row.title"
)

GET_STATE = (
    "MATCH (n:State {id: $id}) WHERE n.ns = $ns "
    "RETURN n.id AS id, n.site AS site, n.sig AS sig, n.url_tmpl AS url_tmpl, n.title AS title"
)

# A transition is an action node between two ACT edges: (from)-[:ACT {cost}]->(a)-[:ACT {cost: 0}]->(to).
MERGE_TRANSITION_IN = "MERGE (s {id: $from_state})-[:ACT]->(a {id: $tid})"
MERGE_TRANSITION_OUT = "MERGE (a {id: $tid})-[:ACT]->(t {id: $to_state})"
SET_TRANSITION_FACTS = (
    "MATCH (a {id: $tid}) SET a:Action, a.ns = $ns, a.site = $site, "
    "a.src = $from_state, a.dst = $to_state, a.action = $action_json"
)
SET_OUT_EDGE_COST = "MATCH (a {id: $tid})-[r:ACT]->(t {id: $to_state}) SET r.cost = 0.0"

# Evidence: one Event node per event id (ADR-0001). Counters are sums over events.
EVENT_EXISTS = "MATCH (e:Event {id: $eid}) RETURN count(e.id) AS total"
MERGE_EVENT = "MERGE (a {id: $tid})-[:EV]->(e {id: $eid})"
SET_EVENT_FACTS = (
    "MATCH (e {id: $eid}) SET e:Event, e.ns = $ns, e.ok = $ok, e.fail = $fail, e.ms = $ms, e.at = $at"
)
TRANSITION_TOTALS = (
    "MATCH (a {id: $tid})-[:EV]->(e) RETURN sum(e.ok) AS ok, sum(e.fail) AS fail, sum(e.ms) AS ms_total"
)
SET_TRANSITION_STATS = "MATCH (a {id: $tid}) SET a.ok = $ok, a.fail = $fail, a.ms = $ms, a.cost = $cost"
SET_TRANSITION_LAST_OK = "MATCH (a {id: $tid}) SET a.last_ok_at = $at"
SET_IN_EDGE_COST = "MATCH (s {id: $from_state})-[r:ACT]->(a {id: $tid}) SET r.cost = $cost"

GET_TRANSITION = (
    "MATCH (a:Action {id: $tid}) WHERE a.ns = $ns "
    "RETURN a.id AS id, a.site AS site, a.src AS src, a.dst AS dst, a.action AS action, "
    "a.ok AS ok, a.fail AS fail, a.ms AS ms, a.cost AS cost, a.last_ok_at AS last_ok_at"
)

MERGE_GOAL_LINK = "MERGE (g {id: $gid})-[:AT]->(a {id: $tid})"
SET_GOAL_FACTS = "MATCH (g {id: $gid}) SET g:Goal, g.ns = $ns, g.site = $site, g.text = $text"
GOAL_TARGETS = "MATCH (g:Goal {id: $gid})-[:AT]->(a) WHERE g.ns = $ns RETURN a.id AS id ORDER BY a.id"

# Flow changes (increment 3): (:Change)-[:CHANGED]->(:Action). 0 stands for "not known".
MERGE_CHANGE = "MERGE (c {id: $cid})-[:CHANGED]->(a {id: $tid})"
SET_CHANGE_FACTS = (
    "MATCH (c {id: $cid}) SET c:Change, c.ns = $ns, c.site = $site, c.tid = $tid, c.src = $from_state, "
    "c.expected = $expected_state, c.action = $action_json, c.first_seen = $first_seen, "
    "c.actual = $actual_state, c.replacement = $replacement_id"
)
GET_CHANGE = (
    "MATCH (c:Change {id: $cid}) WHERE c.ns = $ns "
    "RETURN c.id AS id, c.site AS site, c.tid AS tid, c.src AS src, c.expected AS expected, "
    "c.action AS action, c.first_seen AS first_seen, c.actual AS actual, c.replacement AS replacement"
)
LIST_CHANGES = (
    "MATCH (c:Change) WHERE c.ns = $ns AND c.site = $site "
    "RETURN c.id AS id, c.site AS site, c.tid AS tid, c.src AS src, c.expected AS expected, "
    "c.action AS action, c.first_seen AS first_seen, c.actual AS actual, c.replacement AS replacement"
)

# Map sizes per site (MCP resource atlas://sites). Sites without transitions are absent from
# the second result: default them to 0 in the adapter.
SITE_STATE_COUNTS = "MATCH (n:State) WHERE n.ns = $ns RETURN n.site AS site, count(n.id) AS total"
SITE_TRANSITION_COUNTS = "MATCH (a:Action) WHERE a.ns = $ns RETURN a.site AS site, count(a.id) AS total"

# Skills (increment 7, ADR-0006). A skill node sits between two ACT edges like an action node,
# so the planning query crosses it in two edges. `via` is a JSON list of transition ids.
# `run` on an event is '' when the caller gave no run id; filter those out in the adapter.
SET_EVENT_RUN = "MATCH (e {id: $eid}) SET e.site = $site, e.tid = $tid, e.run = $run"
RUN_STEPS = (
    "MATCH (e:Event) WHERE e.ns = $ns AND e.site = $site "
    "RETURN e.run AS run, e.tid AS tid, e.ok AS ok, e.at AS at"
)
MERGE_SKILL_IN = "MERGE (s {id: $from_state})-[:ACT]->(k {id: $kid})"
MERGE_SKILL_OUT = "MERGE (k {id: $kid})-[:ACT]->(t {id: $to_state})"
SET_SKILL_FACTS = (
    "MATCH (k {id: $kid}) SET k:Skill, k.ns = $ns, k.site = $site, k.src = $from_state, "
    "k.dst = $to_state, k.via = $via_json, k.runs = $runs, k.cost = $cost"
)
SET_SKILL_IN_COST = "MATCH (s {id: $from_state})-[r:ACT]->(k {id: $kid}) SET r.cost = $cost"
SET_SKILL_OUT_COST = "MATCH (k {id: $kid})-[r:ACT]->(t {id: $to_state}) SET r.cost = 0.0"
GET_SKILL = (
    "MATCH (k:Skill {id: $kid}) WHERE k.ns = $ns "
    "RETURN k.id AS id, k.site AS site, k.src AS src, k.dst AS dst, k.via AS via, k.runs AS runs, k.cost AS cost"
)
LIST_SKILLS = (
    "MATCH (k:Skill) WHERE k.ns = $ns AND k.site = $site "
    "RETURN k.id AS id, k.site AS site, k.src AS src, k.dst AS dst, k.via AS via, k.runs AS runs, k.cost AS cost"
)
DELETE_SKILL = "MATCH (k:Skill {id: $kid}) WHERE k.ns = $ns DETACH DELETE k"
UPSERT_LOG = (
    "UNWIND $rows AS row MERGE (l {id: row.id}) SET l:SleepLog, l.ns = row.ns, l.site = row.site, "
    "l.kind = row.kind, l.skill = row.skill, l.via = row.via, l.evidence = row.evidence, l.at = row.at"
)
LIST_LOG = (
    "MATCH (l:SleepLog) WHERE l.ns = $ns AND l.site = $site "
    "RETURN l.id AS id, l.site AS site, l.kind AS kind, l.skill AS skill, l.via AS via, "
    "l.evidence AS evidence, l.at AS at"
)

LIST_GOALS = (
    "MATCH (g:Goal) WHERE g.ns = $ns AND g.site = $site "
    "RETURN g.id AS id, g.site AS site, g.text AS text ORDER BY g.id"
)

# Entities already resolved: (:Entity)-[:SEEN_AT]->(:Action). Every sighting keeps its edge;
# `last_seen` names the latest transition and is what recall reads.
MERGE_ENTITY_SEEN = "MERGE (e {id: $eid})-[:SEEN_AT]->(a {id: $tid})"
SET_ENTITY_FACTS = (
    "MATCH (e {id: $eid}) SET e:Entity, e.ns = $ns, e.site = $site, e.type = $type, e.key = $key, "
    "e.last_seen = $tid, e.seen_at = $at"
)
GET_ENTITY = (
    "MATCH (e:Entity {id: $eid}) WHERE e.ns = $ns "
    "RETURN e.id AS id, e.site AS site, e.type AS type, e.key AS key, "
    "e.last_seen AS last_seen, e.seen_at AS seen_at"
)

MAP_STATES = (
    "MATCH (n:State) WHERE n.ns = $ns AND n.site = $site "
    "RETURN n.id AS id, n.site AS site, n.sig AS sig, n.url_tmpl AS url_tmpl, n.title AS title ORDER BY n.id"
)
MAP_TRANSITIONS = (
    "MATCH (a:Action) WHERE a.ns = $ns AND a.site = $site "
    "RETURN a.id AS id, a.site AS site, a.src AS src, a.dst AS dst, a.action AS action, "
    "a.ok AS ok, a.fail AS fail, a.ms AS ms, a.cost AS cost, a.last_ok_at AS last_ok_at ORDER BY a.id"
)


def recent_outcomes(limit: int) -> str:
    """Latest events of a transition, newest first. Parameter: `$tid`. `at` is an ISO-8601 UTC
    string, so text order is time order; the validated limit is inlined."""
    count = safe_int(limit, 1, 50)
    return f"MATCH (a {{id: $tid}})-[:EV]->(e) RETURN e.ok AS ok, e.at AS at ORDER BY at DESC LIMIT {count}"


def safe_int(value: object, low: int, high: int) -> int:
    """Return `value` if it is a plain int within [low, high]; otherwise raise.

    The one gate for anything inlined into Cypher text. `bool` is rejected although it
    subclasses `int`, and so is every string, however numeric it looks.
    """
    if type(value) is not int or not low <= value <= high:
        raise UnsafeQueryValueError(f"expected an integer in [{low}, {high}]")
    return value


def edges_for_steps(max_steps: int) -> int:
    """`maxLen` for a route of at most `max_steps` transitions.

    Each transition is two ACT edges, and the path ends on the target's action node,
    so n steps are 2n - 1 edges. At most `MAX_PATH_EDGES // 2` (8) steps fit one query.
    """
    return 2 * safe_int(max_steps, 1, MAX_PATH_EDGES // 2) - 1


def sppaths(max_steps: int, path_count: int) -> str:
    """The planning query. Parameters: `$source` (state id), `$target` (transition id).

    The two bounds are inlined after validation so the statement text is one of a small
    fixed set (the engine caches parsed statements). The caller must still order the
    returned paths by cost: the engine returns them by hop count, not by cost.
    """
    max_len = edges_for_steps(max_steps)
    count = safe_int(path_count, 1, MAX_PATH_COUNT)
    return (
        "CALL algo.SPpaths({sourceNode: $source, targetNode: $target, "
        f"relTypes: ['{REL_ACT}'], relDirection: 'outgoing', "
        f"maxLen: {max_len}, pathCount: {count}, costProp: 'cost'}}) "
        "YIELD path, pathCost RETURN path, pathCost"
    )
