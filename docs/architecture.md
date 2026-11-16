# Atlas architecture (Release 0)

Atlas answers one question for a browser agent: *from this page, what is the cheapest reliable
route to that goal?* The map lives in HydraDB; planning is one `algo.SPpaths` query.

## Components

```
 runner / UI / MCP / any agent          (clients; the runner uses only the Route API)
            | HTTP
        atlas/api          thin: validate (contracts), auth, map errors, call core
            |
        atlas/core         pure logic, no I/O: signature, ids, cost, params, locate, planner, learner
            | GraphStore port
  atlas/adapters/hydradb   store.py (Bolt) | fake.py (in-memory) | queries.py (all Cypher) | paths.py
  atlas/adapters/browser   LocalPlaywright  (BrowserDriver port; used by the runner only)
  atlas/adapters/model     AnthropicModelClient (ModelClient port; exploration only)
```

Ports (`atlas/contracts/ports.py`): `GraphStore`, `BrowserDriver`, `ModelClient`, `RouteApi`, `Clock`.
Dependencies point inwards: `api`, `runner`, `adapters` -> `core` -> `contracts`. `core` imports
nothing that does I/O. Types, API models and errors live only in `atlas/contracts/`.

## Data flow

- **locate** `POST /v1/locate {site, snapshot}`: `signature(snapshot)` -> state id -> `store.upsert_state`
  -> `{state_id, known}`.
- **plan** `POST /v1/plan {site, state_id, goal_text, params}`: `goal_key` (parameter values replaced by
  `{slot}`) -> `store.goal_targets` -> `store.shortest_paths` to each target, widening the step bound up
  to the cap -> first cheapest path whose slots the caller can fill -> `{route, confidence, needs_model,
  reason, max_steps_used}`. No model call.
- **report** `POST /v1/report {site, from_state, action, to_snapshot, ok, ms, event_id, ...}`: locate
  `to_snapshot`; templatise the action; record one `Observation` per affected transition (idempotent on
  `event_id`); the store recomputes `ok/fail/ms/cost`; if `goal_reached`, link the goal to the transition.
- **runner loop**: goto start -> locate -> plan -> drive route steps (report each, no model) -> where
  `needs_model`, ask the model for one action, act, report -> when the model says done (or the route's
  last step lands as expected) re-send the last event with `goal_reached=true`.

## Graph model (ADR-0003)

`(:State)-[:ACT {cost}]->(:Action {action, ok, fail, ms, cost, src, dst})-[:ACT {cost: 0}]->(:State)`;
`(:Action)-[:EV]->(:Event {ok, fail, ms})` (ADR-0001); `(:Goal)-[:AT]->(:Action)`. A goal points at the
*action that achieves it*, so a task whose final page equals its starting page still has a route.
Namespace per tenant is hashed into every node id and stored as `ns` (see `queries.py` docstring).

## Public signatures implementers must satisfy (the tests call exactly these)

```python
# atlas/core/ids.py
stable_id(*parts: str | int) -> int                      # in [1, MAX_ID], 53-bit
state_id(ns: str, site: str, sig: str) -> int
action_aid(action: Action) -> str
transition_id(ns: str, from_state: int, action: Action, to_state: int) -> int
goal_id(ns: str, site: str, goal_key: str) -> int
event_node_id(ns: str, event_id: str) -> int
# atlas/core/signature.py  (rules: ADR-0002)
url_template(url: str) -> str
signature(snapshot: Snapshot) -> str                     # 64 hex chars
state_from_snapshot(ns: str, site: str, snapshot: Snapshot) -> State
# atlas/core/cost.py
edge_cost(ok: int, fail: int, ms: float) -> float        # -ln((ok+1)/(ok+fail+2)) + ms/10000
reliability(ok: int, fail: int) -> float                 # (ok+1)/(ok+fail+2)
# atlas/core/params.py
goal_key(goal_text: str, params: Mapping[str, str]) -> str
templatise_action(action: Action, params: Mapping[str, str]) -> Action   # values -> {slot}
required_slots(action: Action) -> frozenset[str]
bind_action(action: Action, params: Mapping[str, str]) -> Action         # {slot} -> values; MissingParamError
# atlas/core/locate.py, planner.py, learner.py
locate(store: GraphStore, ns: str, site: str, snapshot: Snapshot) -> LocateResult
plan(store: GraphStore, ns: str, request: PlanRequest, *, max_steps: int = 8,
     max_steps_cap: int = 8, path_count: int = 5) -> PlanResult          # bounds clamped to MAX_ROUTE_STEPS
report(store: GraphStore, clock: Clock, ns: str, event: ReportEvent) -> ReportResult
# atlas/adapters/hydradb/
fake.InMemoryGraphStore()                                 # implements GraphStore
store.open_store(url: str, token: str, graph: str) -> HydraGraphStore    # GraphStore + close()
paths.parse_path_rows(rows: Sequence[Mapping[str, object]]) -> tuple[RawPath, ...]
paths.RawPath(node_ids: tuple[int, ...], cost: float)     # PathFormatError on bad input
queries.safe_int / edges_for_steps / sppaths              # implemented
# atlas/api/app.py
create_app(store: GraphStore, clock: Clock, settings: Settings) -> FastAPI
# atlas/runner/, atlas/adapters/browser/, atlas/adapters/model/
client.HttpRouteApi(base_url: str, api_key: str | None)   # implements RouteApi
run.run_task(task: TaskSpec, routes: RouteApi, driver: BrowserDriver, model: ModelClient, *,
             secrets: Mapping[str, str] | None = None, max_steps: int = 25,
             on_step: Callable[[StepLog], None] | None = None) -> RunResult
local_playwright.LocalPlaywright(base_url: str, headless: bool = True)   # BrowserDriver, context manager
client.AnthropicModelClient(api_key: str, model_id: str)  # implements ModelClient
```

Behaviour the signatures do not show:
- `plan`: unknown goal -> `UNKNOWN_GOAL`; search bound doubles from `max_steps` to `max_steps_cap`;
  nothing at the cap -> `NO_ROUTE_WITHIN_MAX_STEPS` with `max_steps_used == cap`; paths exist but need
  slots missing from `params` -> `MISSING_PARAMS`. Paths are re-sorted by cost in core.
- `report`: `ok` and (no `expect_state` or it matches) -> one success on `(from, action, to)`.
  `expect_state` given and not reached -> one failure on `(from, action, expect_state)`, plus a success on
  the actual transition when `ok`. `ok=False` without `expect_state` records nothing. `duplicate` is true
  when `event_id` was already recorded; counters do not change, but `goal_reached` still links the goal.
- `HydraGraphStore.shortest_paths` must check that `source` belongs to `ns` before querying.

## MCP server (contract in `atlas/contracts/mcp_models.py`)

`atlas/mcp/tools.py` holds the behaviour (`AtlasTools`, one instance per agent session, pure Python on
core + `MapStore` + an optional `BrowserDriver`); `budget.py` enforces the response budget; `server.py`
only registers `tools_for(mode)` with the MCP SDK (stdio and streamable HTTP at `/mcp`).

```python
# atlas/mcp/tools.py
AtlasTools(store: MapStore, clock: Clock, ns: str, site: str,
           driver: BrowserDriver | None = None, *, max_steps: int = 25)
  .find_skills(FindSkillsInput) -> FindSkillsOutput     .recall(RecallInput) -> RecallOutput
  .run(RunInput) -> RunOutput                           .act(ActInput) -> RunOutput
  .plan(PlanToolInput) -> PlanToolOutput                .report(ReportToolInput) -> ReportToolOutput
# atlas/mcp/budget.py
enforce_budget(result: T) -> T          # <= 400 tokens; cut lists from the end, then free text; truncated=True
# atlas/mcp/server.py
build_server(tools: AtlasTools, mode: Mode) -> Server                      # stdio
build_http_app(tools: AtlasTools, mode: Mode, settings: Settings) -> ASGI  # UnauthorizedError if no key
# atlas/core/ids.py (added)             entity_id(ns, site, type, key) == stable_id(ns, "entity", site, type, key)
# both stores (added): the ContextStore port: list_goals, get_transition, record_entity, get_entity
```

- Budget: 400 tokens per response, tokens = ceil(characters / 3) of `tool_text(result)`.
- Runner session state (goal, params, offered choices, pending fills, last event) lives on the
  `AtlasTools` instance; the map lives only in the store.
- Entities: after every successful report, each task parameter whose slot the action uses becomes
  `(:Entity)-[:SEEN_AT]->(:Action)`; recall plans a path to that action and binds it.
- Skills are goals for now (`skill_id = "goal:<id>"`), ranked by word overlap with the goal text in
  Python; SKILL edges join the same list once skills are stored.

## Flow changes and self-repair

```python
# atlas/core/changes.py
is_flow_change(transition: Transition, recent: Sequence[bool]) -> bool   # ok >= 5 and two newest failed
note_report(store: ChangeStore, clock: Clock, ns: str, result: ReportResult) -> ChangeEvent | None
# both stores: the ChangeStore port (recent_outcomes, record_change, list_changes)
# change id == stable_id(ns, "change", transition_id)
# API: GET /v1/changes?site= -> ChangesResponse; POST /v1/report calls note_report after learner.report
```

`note_report` skips duplicates. For each transition in the report result it reads the two newest
outcomes; on a change it records a `ChangeEvent`, and when the same report also produced a success on
another transition (the action landed elsewhere) that one is the replacement. In the runner, a mapped
step that cannot execute is reported with `ok=false` and its `expect_state`, as in MCP runner mode.

The exploration model is chosen by settings (`MODEL_PROVIDER`, `MODEL_BASE_URL`, `MODEL_API_KEY`,
`ATLAS_MODEL_ID`); an OpenAI-compatible provider is called over httpx, with no new dependency.

## Release 0 scope

In: TestMart; signatures; Route API (`locate`, `plan`, `report`, `GET /v1/map/{site}`, `/healthz`);
planner; learner with event-sourced counters; fake + HydraDB store; reference runner on local Playwright;
one exploration model call per frontier step; minimal UI; deploy. One tenant per deployment
(`ATLAS_NAMESPACE`), one bearer key (`ATLAS_API_KEY`).

Out of scope for this release: goal embeddings, SKILL / FRONTIER / Entity, replan-on-mismatch in the runner beyond a
single replan, mutation and change events, MCP, sleep phase, multi-tenant keys, `State.visits`.

## Design deviations

| Deviation | Reason |
|---|---|
| `atlas/runner/`, `atlas/settings.py`, `atlas/demo/` inside the package | one installable package; the original layout was ambiguous |
| `core/ids.py`, `core/params.py`, `core/locate.py` added; `core/changes.py` deferred | one job per module; change detection is not Release 0 |
| `adapters/hydradb/paths.py` added | path parsing is tested on its own against the real fixture |
| Transitions are action nodes, goals point at actions (ADR-0003) | parallel edges collapse under MERGE; page-as-goal cannot express "edit and return" |
| An `Action` is `fills` + one trigger; `Locator.row`; kind `check` | form fields do not change the state, so they cannot be edges of their own |
| Route steps nest `action`; report adds `expect_state`, `goal_text`, `params`, `goal_reached` | the caller echoes a step back verbatim; goals must be learnable through the three calls |
| 53-bit ids instead of 63-bit (ADR-0002) | exact in JSON and JavaScript for the UI |
| Goal resolution is exact match on the templated goal text | embeddings are a later addition; composition already works across start states |
| Python `>=3.12,<3.14`; pip + venv, no uv | 3.12 pinned in Docker, 3.13 is what the dev machine has; one less tool to install |
| Sync ports and sync FastAPI handlers | neo4j and Playwright sync APIs; simplest code; FastAPI runs them in its thread pool |

## Engine facts the design rests on (verified on the real engine)

1. `algo.SPpaths` minimises `costProp` and returns its sum as `pathCost`; `weightProp` is silently
   ignored. Rows come back by hop count, not by cost: core always re-sorts.
2. A path query may span at most 16 edges, so one query returns a route of at most 8 steps (ADR-0005);
   skills extend that reach (ADR-0006).
3. Path nodes carry their id as the element id, not as a property; relationship ids in paths are
   synthetic. The adapter reads node element ids and loads each action or skill node by id.
4. `MERGE` by id then a separate `MATCH ... SET` is the only upsert shape; an edgeless node needs the
   `UNWIND $rows ... MERGE ... SET` batch form. A node created as an edge endpoint accepts a later `SET`.
5. Write counters in query summaries are unreliable: the adapter never uses them; it reads back.
6. `sum()` over no rows is 0. Scalar parameters work in path procedures; the two bounds are inlined
   after validation.
7. The saved response in `tests/fixtures/sppaths_response.json` has the shape the path-parsing test
   documents, in engine order (deliberately unsorted).
