# ADR-0001: Idempotent writes keyed by event id

Status: accepted (Release 0).

## Context
Many agents report to one map, and callers retry. HydraDB has no transactions (every statement
auto-commits), `SET r.ok = r.ok + 1` style increments are not something we can rely on, and a
read-modify-write of a counter loses updates under concurrency.

## Decision
Counters are never incremented. Each report becomes evidence:

1. `MERGE (action {id: tid})-[:EV]->(event {id: event_node_id(ns, event_id)})`, then
   `MATCH (event) SET event.ok, event.fail, event.ms, event.at`. MERGE by id makes a retry a no-op.
2. Totals are derived: `MATCH (a {id: tid})-[:EV]->(e) RETURN sum(e.ok), sum(e.fail), sum(e.ms)`.
   `ms` is `sum(ms of successes) / ok`.
3. `cost = edge_cost(ok, fail, ms)` is written to the action node and to its incoming `ACT` edge.

A set union is commutative and idempotent, so concurrent reporters and retries converge to exact counts.
The derived `cost` is last-writer-wins; a stale write is corrected by the next report on that transition.
One report can touch two transitions (expected and actual destination); the failure uses the event id
`<event_id>:exp`, the success uses `<event_id>`.

## Consequences
- `GraphStore.record_observation` is idempotent on `event_id`; `has_event` exposes duplicates.
- Event nodes grow without bound. Compaction (fold old events into a base count) is a sleep-phase job.
- 20-concurrent-reporter exactness is tested on real HydraDB.
