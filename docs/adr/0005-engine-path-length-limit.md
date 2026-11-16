# ADR-0005: The engine's 16-edge path limit and what it means for routes

Status: accepted. Found by running the planning query against the real engine.

## Context
`algo.SPpaths` rejects `maxLen` above 16 (`native_path_max_len rejected by admission control: actual 23
exceeds limit 16`). The limit is the engine's `max_traversal_hops` default and is not something we
configure. Over Bolt the rejection arrives as a transient error, so it looked like an outage. Our first
defaults (12 steps, widening to 48) asked for 23 and more edges, so every plan failed on the real engine
while passing on the in-memory store.

Two more engine facts, verified: paths come back ordered by hop count, not by cost; and `pathCount`
limits how many are returned in that order.

## Decision
1. A transition is two `ACT` edges and a route ends on an action node, so n steps need 2n - 1 edges:
   **one query can return a route of at most 8 steps** (`MAX_ROUTE_STEPS = 8`, 15 edges).
   `queries.sppaths` refuses anything larger before it reaches the engine.
2. The planner clamps its starting bound and every widened bound to `MAX_ROUTE_STEPS`, whatever the
   caller passes. Beyond that it answers `NO_ROUTE_WITHIN_MAX_STEPS` with `max_steps_used = 8`, and the
   agent explores from there; each explored step is learned, so the frontier moves forward.
3. Core always re-sorts returned paths by summed cost. Because the engine returns the fewest-hop paths
   first, a cheaper but longer path can be missing when more than `path_count` shorter ones exist; we
   ask for several paths (5 by default) and accept this as a known gap.
4. Longer flows are the job of skills: a proven multi-step path compressed into one skill
   node costs two edges however many steps it replaces, so skills extend the reach of a single query.

## Consequences
- Settings default to `plan_max_steps = 8`, `plan_max_steps_cap = 8`.
- The in-memory store has no such limit, so the limit is enforced in core and in `queries.py`, and
  tested there, rather than discovered only on the engine.
- The adapter should map this rejection to a clear typed error, not "store unavailable".
