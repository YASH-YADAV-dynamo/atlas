# ADR-0006: Skills are shortcut nodes built by the sleep phase from run history

Status: accepted.

## Context
The design calls for proven multi-step paths to be compressed into skills, removed again when a constituent
step breaks, with a change log. ADR-0005 adds a second reason: the engine returns at most 16 edges per
path query, which is 8 route steps, and real flows are longer.

To call a path "driven successfully three times" the map must know which steps belonged to one run.
Reports did not say.

## Decision
1. **Reports may carry `run_id`.** It is optional; steps without it still teach edges but never count as
   evidence for a skill. It is stored on the event node together with the transition id and site.
2. **Candidates** are computed in core from run history, with no model: each run is cut at failed steps
   and at every goal's final transition; a segment of at least 3 transitions that appears identically in
   at least 3 runs becomes a skill. A goal's final transition is never compressed, because a plan must
   end by executing it (ADR-0003).
3. **A skill is a node shaped like an action:**
   `(from:State)-[:ACT {cost}]->(k:Skill {via, runs, cost})-[:ACT {cost: 0}]->(to:State)`.
   The planning query needs no change and crosses a skill in two edges however many steps it holds.
   `cost` is 0.95 times the sum of its transitions' costs, so it wins over the same steps taken one by one.
4. **The store expands skills.** `shortest_paths` returns the skill's transitions in order inside
   `steps` and names the skill in `skill_ids`. The planner, the runner and MCP keep working on plain
   transitions; every step is still executed, verified and reported one by one, so edge statistics and
   self-repair are unchanged.
5. **Removal.** In each sleep run, a skill with a transition whose two newest outcomes are failures is
   deleted (`DETACH DELETE` of the skill node only) and is not re-created while that holds.
6. **Change log.** Every creation and removal is a `SleepLog` node with its evidence, listed by
   `GET /v1/skills`. `POST /v1/sleep?site=` runs the phase on demand; the demo and the benchmark call it
   between iterations.

## Consequences
- With skills, a flow of any length whose prefix has been driven three times is plannable in one query.
- Only whole identical segments are compressed (no sub-segments, no merging of overlapping skills). That
  is a known gap, chosen for determinism.
- Skill costs are a snapshot from the sleep run that created them; they are refreshed when the skill is
  re-created, not on every report.
