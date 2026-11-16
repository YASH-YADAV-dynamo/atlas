# Benchmark results

## smoke-deepseek-20261115 (the only run so far)

| | |
|---|---|
| Date | 2026-11-15 (run finished after midnight, 2026-11-16 local) |
| Command | `python -m eval.run --run-id smoke-deepseek-20261115 --max-usd 1.5` (smoke defaults in `eval/run.py`) |
| Configuration | seed 1 only; 3 iterations x 10 stream tasks; 10 held-out tasks after each iteration (B0, B1, A); M0/M1 run the stream only. 240 task records. Step cap 25. |
| Model | DeepSeek, `ATLAS_MODEL_ID=deepseek-flash`, `MODEL_PROVIDER=openai`, `https://api.deepseek.com`, thinking mode disabled for every arm |
| Route API | in-process on an in-memory map (default; no `--api-url`). Arm M1 starts `python -m atlas.mcp` on the local HydraDB container. |
| Cost | $0.98 by the run's ledger (`run_meta.json`, `partial: false`, limit $1.50). $0.92 summed from the records. Prices used: $0.30 / $1.20 per million input / output tokens (`MODEL_PRICE_*` in `.env`; not yet checked against DeepSeek's price page, see Limitations). |
| Ledger | shared local ledger total after this run, including the two aborted attempts and probes below: $1.06 of the $5 cap |

Files: `records.jsonl` (raw, one task per line), `summary.json` and `summary.csv` (per arm, split,
iteration; derived from the records), `tables.md` (the same as tables, plus per arm/split totals),
`model_errors.log` (one line per task ended by a model error), the four charts.

### Headline numbers (read them with the limits below)

Held-out tasks, last iteration (10 tasks each, learning frozen):

| Arm | Held-out success | Model calls per success | Tokens per success | Zero-model-call share | Wall p50 / p95 ms |
|---|---|---|---|---|---|
| B0 no memory | 7/10 | 10.6 | 18,195 | 0.00 | 5,291 / 61,604 |
| B1 trajectory cache | 9/10 | 2.0 | 3,364 | 0.60 | 417 / 14,613 |
| A Atlas | 6/10 | 1.2 | 1,966 | 0.20 | 1,351 / 23,766 |

Atlas held-out over the 3 iterations: success 5/10, 8/10, 6/10; model calls per success 3.0, 4.4, 1.2.

M arms, stream tasks, mean of the 3 iterations (30 tasks each):

| | Tool calls per task | Context tokens per task | Success | Wall p50 (all 30 tasks) |
|---|---|---|---|---|
| M0 browser tools only | 10.4 | 1,470 | 30/30 | 18.7 s |
| M1 with Atlas MCP | 12.3 | 2,495 | 28/30 | 11.8 s |

**What this run does not show.** In this smoke run B1 (the trajectory cache) beats Atlas on held-out
success (9/10 vs 6/10, and 25/30 vs 19/30 over all held-out tasks) and Atlas does not beat B0 on held-out
success (19/30 vs 21/30). Atlas does use the fewest model calls and tokens per successful held-out task.
M1 did not reduce tool calls or context tokens versus M0; it used more of both. These are one seed and
small samples (10 tasks per cell), so differences of one or two tasks are noise.

### Failures, reported as they happened

- **Model errors: 42 of 180 B0/B1/A tasks** ended because `deepseek-flash` returned a decision the
  client cannot use (29 "invalid decision", 13 "no tool call"): B0 21, A 14, B1 7. M0/M1: 0. Such a task
  counts as a failure (success is still the TestMart DB check). This is the main reason B0 and A
  success rates are low; see `docs/rca/2026-11-15-smoke-crash-extra-json.md` (local, gitignored) for
  captured examples (two actions in one call, a click without a locator).
- **Atlas map steps:** 29 of 168 map-driven steps failed (most after a 10 s locator timeout), leading
  to replans; 4 Atlas tasks ended "stuck". Not investigated in this run.
- **Two earlier attempts were discarded, not cherry-picked:** attempt 1 crashed on its 3rd task (one
  malformed model response stopped the whole harness; 2 records, about $0.01); attempt 2 was stopped by
  hand after 13 tasks (about $0.04) to diagnose model errors. Both led to harness fixes (commits on
  this branch: tolerant parse of trailing data, per-task model errors end the task instead of the run,
  thinking disabled for M arms, a missing `done` flag inferred). Probes and two small capture runs cost
  about $0.03 more. Their records were deleted; this run started from an empty folder.

### Limitations

- One seed. The smoke defaults in code (3 iterations, 10 + 10 tasks) differ from `docs/benchmark.md`
  ("2 iterations, 3 stream + 3 held-out"); the docs are out of date.
- The Atlas arm used the in-process in-memory map, not HydraDB (M1's MCP server did use HydraDB).
- Tokens of a response that failed to parse are not in the records or the ledger, so cost is slightly
  understated.
- Prices: `docs/benchmark.md` still lists Claude Sonnet prices; the dollar figures here use the
  `.env` prices above, which still need checking against DeepSeek's pricing page with a date.
- Synthetic site (TestMart) that we run ourselves.

### A full 3-seed run (not run)

`EVAL_FULL=1`: seeds 1-3, 4 iterations x 10 stream + 10 held-out after each = 80 tasks per seed for
B0/B1/A and 40 for M0/M1, i.e. 4x this smoke run. Estimate from this run: about $3.7-3.9 and
roughly 4x this run's wall time (3-4 hours). With $1.06 already on the shared ledger that would end near the $5
ledger cap; it needs `--max-usd` and a ledger budget decision first.
