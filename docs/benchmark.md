# Benchmark

The benchmark runs locally, never on the hosted demo. It compares five arms on TestMart tasks with the same
model and the same step cap, writes raw results as JSON lines, and draws every chart from those files.

## Arms

| Arm | What it is |
|---|---|
| B0 | Plain model-driven agent, no memory: one model call per step, plus one that says done. |
| B1 | Trajectory cache keyed by task template. Replays the stored action list with new parameters (zero model calls); falls back to B0 on a miss or a broken replay. |
| A | Atlas: the reference runner through the Route API. Learning on in the stream, frozen on the held-out split. |
| M0 | Tool-calling agent with browser tools only (goto, snapshot, click, fill). |
| M1 | M0 plus the Atlas MCP server in runner mode (stdio). |

Success is never the arm's belief: it is the TestMart database check (`Task.check`) after the run.
Each task starts from `POST /__reset?seed=N` with the seed given to `generate_tasks`, and each arm and seed
uses its own `site` name, so maps never mix.

## Running

```
python -m eval.run --run-id smoke-deepseek-20261115 --max-usd 1.5   # smoke: one seed, 3 iterations, 10 stream + 10 held-out tasks after each
make eval EVAL_FULL=1          # full: seeds 1-3, 4 iterations x 10 stream + 10 held-out tasks after each (not run yet)
.venv/Scripts/python -m eval.run --full --arms b0 b1 atlas --api-url http://127.0.0.1:8000
```

Needs a DeepSeek key through the OpenAI-compatible settings: `MODEL_PROVIDER=openai`, `https://api.deepseek.com`, `ATLAS_MODEL_ID=deepseek-flash` (and `make setup`, which installs the `bench` extra). By default the Route API
runs in the process on an in-memory map; `--api-url` uses a running Atlas API (and so HydraDB). Arm M1
starts `python -m atlas.mcp`, which uses the HydraDB settings in the environment.
Output: `results/<run-id>/records.jsonl` (one TaskRecord per line), `summary.json` (derived from the
records) and the charts, which `python -m eval.plot results/<run-id>` redraws from `records.jsonl` alone:
`heldout_success.png`, `cost_stream.png`, `cost_heldout.png`, `mcp_round_trips_context.png`.
Several seeds are drawn as the mean with a min/max band.

## What is counted

- Model calls, input and output tokens: the provider's reported usage, summed per task. No estimates.
- Tool calls (round trips) and context tokens (arms M0/M1 only): tool definitions once plus every tool
  result, counted with `count_tokens` (ceil(characters / 3), stricter than 4 characters per token). This is
  a deterministic estimate; the provider's input tokens are reported next to it.
- Cost per successful task counts only successful tasks; success rate counts all.
- Records hold no page text, prompts or secrets.

## Prices

Dollar figures use one price list, stated here and nowhere else.

| Model | Input | Output | Source | Date |
|---|---|---|---|---|
| DeepSeek `deepseek-flash` | $0.30 per million tokens | $1.20 per million tokens | `MODEL_PRICE_*` in `.env`; not yet checked against DeepSeek's pricing page | not yet verified |

Cache writes and cache hits are not used by the benchmark and are not priced. Dollars per task =
(input tokens x 0.30 + output tokens x 1.20) / 1,000,000. If the provider or model changes, update this table.

## Reading the results

Results are reported as they come, including where B1 beats Atlas (an exact template repeated with new
parameters is B1's best case). Held-out tasks use the same templates from start pages the stream never
uses, so only a map that can join learned paths succeeds without model calls.
