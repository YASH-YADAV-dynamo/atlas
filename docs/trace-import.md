# Trace import

Turn recorded agent runs into an Atlas map without re-exploring. A trace is a JSON Lines file: one
`TraceStep` object per line, UTF-8, blank lines ignored. Importing replays each step through the Route
API (`locate`, then `report`), so the map learns the pages and actions the agent already used.

## Schema (closed: unknown fields are rejected)

| Field | Type | Notes |
|---|---|---|
| `run_id` | string | Same value for every step of one run. |
| `index` | integer 0..9999 | Step number within the run. `run_id` + `index` make the import idempotent. |
| `site` | string | Site name the map belongs to. |
| `before` | Snapshot | The page before the action. |
| `action` | Action | The action taken, with the concrete values used. |
| `after` | Snapshot | The page after (the page before again if the action failed). |
| `ok` | boolean, default true | The action executed. A failed step is counted but teaches no edge. |
| `ms` | integer, default 0 | Duration in milliseconds. |
| `goal_text` | string, optional | The task goal. |
| `params` | object of strings | Parameter values used; the import replaces them by `{slot}` placeholders, so the route serves other values. |
| `goal_reached` | boolean, default false | On a run's last step: links `goal_text` to it. Ignored on a failed step. |

`Snapshot` and `Action` are the Route API types (the machine-readable schema is
`TraceStep.model_json_schema()`). One line, as it appears in the file:

```json
{"run_id":"agent-run-0001","index":0,"site":"shop","before":{"url":"https://shop.example/","title":"Home","elements":[{"role":"link","name":"Orders"}],"text":"Home"},"action":{"kind":"click","locator":{"role":"link","name":"Orders"}},"after":{"url":"https://shop.example/orders","title":"Orders","elements":[],"text":"Orders"},"ok":true,"ms":120,"goal_text":"Refund order 1042","params":{"order_id":"1042"},"goal_reached":false}
```

## Commands

Import a file (uses `ATLAS_API_URL` and `ATLAS_API_KEY`):

```
python -m atlas.import_trace runs.jsonl
```

It prints one JSON line, for example
`{"steps":5,"runs":1,"new_states":6,"duplicates":0,"failed_steps":0,"goals_linked":1}`.
The whole file is checked first: a bad line prints `trace format error: line N: ...` to stderr, exits
with code 2 and imports nothing. Re-importing the same file counts every step as a duplicate.

## Recording from any agent

`TraceRecorder` wraps any browser driver (an object with `snapshot()`, `act(action)` and `url()`) and
records each `act`:

```python
from pathlib import Path
from atlas.import_trace import TraceRecorder

recorder = TraceRecorder(
    driver, site="shop", run_id="agent-run-0001", goal_text="Refund order 1042", params={"order_id": "1042"}
)
# give `recorder` to your agent in place of `driver`, then:
recorder.write(Path("runs.jsonl"), goal_reached=True)  # appends one line per step
```

Traces store actions as given. Never pass real credentials: use templated actions for logins
(for example a password field filled with `{password}`).
