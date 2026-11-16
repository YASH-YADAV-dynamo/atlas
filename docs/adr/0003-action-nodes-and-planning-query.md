# ADR-0003: Transitions as action nodes; the planning query

Status: accepted (Release 0).

## Context
- HydraDB's `MERGE` de-duplicates a relationship on (source, destination, type). Two different actions
  between the same two states (the top-nav link and the dashboard shortcut) would collapse into one
  edge, and plain `CREATE` duplicates on every retry.
- Path results expose `src`, `dst` and type, not a usable relationship id, so parallel edges could not
  be told apart in a returned path anyway.
- A goal defined as "be on page X" cannot express "edit the address": the task ends on the page it
  started from.
- Filling a form field does not change the state signature, and shortest paths are simple (no state is
  visited twice), so a fill can never be an edge of its own.

## Decision
1. **A transition is a node.** `(from:State)-[:ACT {cost}]->(a:Action)-[:ACT {cost: 0.0}]->(to:State)`.
   `a.id = transition_id(ns, from, action, to)`. The action node carries `action` (JSON), `ok`, `fail`,
   `ms`, `cost`, `src`, `dst`, `last_ok_at`; the incoming edge carries the same `cost` for the engine.
   Both edges are written with idempotent `MERGE` by id, then `SET`.
2. **A goal points at the action that achieves it:** `(:Goal)-[:AT]->(:Action)`. Planning asks for paths
   from the current state to that action node, so the final action is always part of the route.
3. **An action is `fills` + one trigger.** Form fields set before the trigger travel inside the action
   (`Action.fills`); the model returns such a compound step in one call.
4. **Planning query** (one statement, in `queries.sppaths`):

   ```cypher
   CALL algo.SPpaths({sourceNode: $source, targetNode: $target, relTypes: ['ACT'],
                      relDirection: 'outgoing', maxLen: <2*steps-1>, pathCount: <k>, costProp: 'cost'})
   YIELD path, pathCost RETURN path, pathCost
   ```

   `maxLen` counts edges: a route of n transitions ending on the target action node is `2n - 1` edges.
   The two bounds are inlined after `safe_int`; ids are scalar parameters. The adapter turns each path
   into `Transition`s (every second node), core re-sorts by summed cost and never trusts row order.
5. **A too-small bound is never silent.** The engine cannot tell "no path" from "bound too small", so
   `plan` doubles the step bound from `max_steps` up to `max_steps_cap` and reports `max_steps_used`;
   giving up returns `NO_ROUTE_WITHIN_MAX_STEPS`, distinct from `UNKNOWN_GOAL`.

## Consequences
- Twice the edges per step and one extra node; fine at our scale, and SKILL edges (later) can join
  states directly with the same relationship type.
- Per-action statistics have one obvious home, and the UI can draw action nodes or collapse them.
- `k` paths are requested so core can skip routes whose `{slot}`s the caller cannot fill.
