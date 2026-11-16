"""`python -m eval.run [--full]`: run the benchmark arms locally and write results/<run-id>/.
The default is a small smoke configuration (`make eval`); `--full` or EVAL_FULL=1 runs all seeds.

Per seed and arm: an experience stream in iterations (learning on), and after each iteration the
held-out compositions (learning frozen). Each task starts from a freshly reset TestMart. The run
needs a model key (ANTHROPIC_API_KEY, or MODEL_API_KEY with MODEL_PROVIDER=openai);
token counts are the provider's usage numbers.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from atlas.adapters.browser.local_playwright import LocalPlaywright
from atlas.adapters.model.factory import build_model_client, model_api_key, model_base_url
from atlas.adapters.model.ledger import BudgetedToolModel, ledger_from_settings
from atlas.contracts.bench_models import Arm, Split, TaskRecord, ToolModel
from atlas.contracts.errors import ModelUnavailableError
from atlas.contracts.ports import ModelClient, RouteApi
from atlas.contracts.types import Action, ActionKind
from atlas.runner.client import HttpRouteApi
from atlas.settings import Settings
from eval.arms import ArmContext, TrajectoryCache, run_atlas, run_b0, run_b1
from eval.budget import run_within_budget, write_meta
from eval.mcp_arms import McpBridge, atlas_server_params, browser_tools, run_m
from eval.models import AnthropicToolModel, OpenAIToolModel
from eval.plot import draw_all
from eval.results import append_records, read_records, summarise
from eval.stack import FrozenRoutes, InProcessRoutes, TestMart, sign_in
from eval.tasks import EvalTask, build_tasks

ALL_ARMS = (Arm.B0, Arm.B1, Arm.ATLAS, Arm.M0, Arm.M1)


@dataclass(frozen=True)
class RunConfig:
    run_id: str
    seeds: tuple[int, ...]
    iterations: int
    stream_per_iteration: int
    heldout: int
    arms: tuple[Arm, ...]
    out_dir: Path
    testmart_port: int = 8101
    api_url: str | None = None  # None: Route API in this process on an in-memory map
    max_steps: int = 25
    max_usd: float | None = None


def parse_args(argv: Sequence[str]) -> RunConfig:
    p = argparse.ArgumentParser(prog="python -m eval.run")
    p.add_argument("--full", action="store_true", help="the full configuration (default: smoke)")
    p.add_argument("--run-id", default=time.strftime("run-%Y%m%d-%H%M%S"))
    p.add_argument("--seeds", type=int, nargs="+", default=[1, 2, 3])
    p.add_argument("--iterations", type=int, default=4)
    p.add_argument("--stream-per-iteration", type=int, default=10)
    p.add_argument("--heldout", type=int, default=10)
    p.add_argument(
        "--arms", nargs="+", choices=[a.value for a in ALL_ARMS], default=[a.value for a in ALL_ARMS]
    )
    p.add_argument("--out", type=Path, default=Path("results"))
    p.add_argument("--testmart-port", type=int, default=8101)
    p.add_argument("--api-url", default=None, help="a running Atlas API (default: in-process, in-memory map)")
    p.add_argument("--max-steps", type=int, default=25)
    p.add_argument("--max-usd", type=float, default=None, help="stop cleanly once this much was spent")
    a = p.parse_args(argv)
    if not (a.full or os.environ.get("EVAL_FULL") == "1"):  # smoke: a few tasks per arm, one seed
        a.seeds, a.iterations, a.stream_per_iteration, a.heldout = [1], 3, 10, 10
    return RunConfig(
        run_id=a.run_id, seeds=tuple(a.seeds), iterations=a.iterations,
        stream_per_iteration=a.stream_per_iteration, heldout=a.heldout,
        arms=tuple(Arm(x) for x in a.arms), out_dir=a.out / a.run_id, testmart_port=a.testmart_port,
        api_url=a.api_url, max_steps=a.max_steps, max_usd=a.max_usd,
    )  # fmt: skip


def run_arm(
    cfg: RunConfig, arm: Arm, seed: int, site: str, settings: Settings, mart: TestMart,
    model: ModelClient, tool_model: ToolModel, tasks: tuple[list[EvalTask], list[EvalTask]],
    spent: Callable[[], float],
) -> bool:  # fmt: skip
    """Run one arm of one seed; True when it stopped early at the spending limit."""
    experience, held = tasks
    records_path = cfg.out_dir / "records.jsonl"
    routes: RouteApi = InProcessRoutes()
    if cfg.api_url:
        key = settings.atlas_api_key.get_secret_value() if settings.atlas_api_key else None
        routes = HttpRouteApi(cfg.api_url, key)
    cache = TrajectoryCache()
    with LocalPlaywright(mart.url) as driver:
        sign_in(driver, settings)
        bridge = None
        tools = browser_tools(driver)
        if arm is Arm.M1:
            env = {**os.environ, "TESTMART_URL": mart.url, "MCP_SITE": site, "MCP_MODE": "runner"}
            bridge = McpBridge(atlas_server_params(env)).__enter__()
            tools = {**tools, **bridge.tools()}
        try:
            for iteration in range(cfg.iterations):
                chunk = experience[
                    iteration * cfg.stream_per_iteration : (iteration + 1) * cfg.stream_per_iteration
                ]
                phases = [(Split.STREAM, chunk, routes, cache)]
                if arm in (Arm.B0, Arm.B1, Arm.ATLAS):
                    phases.append((Split.HELDOUT, held, FrozenRoutes(routes), cache.copy()))
                for split, batch, phase_routes, phase_cache in phases:

                    def one(
                        item: EvalTask, split: Split = split, routes: RouteApi = phase_routes,
                        cache: TrajectoryCache = phase_cache, iteration: int = iteration,
                    ) -> TaskRecord:  # fmt: skip
                        mart.reset(seed)
                        ctx = ArmContext(
                            run_id=cfg.run_id, seed=seed, iteration=iteration, split=split, driver=driver,
                            model=model, check=lambda ans, t=item: t.check(mart.db_path, ans),
                            routes=routes, cache=cache, max_steps=cfg.max_steps,
                        )  # fmt: skip
                        record = run_one(arm, item, ctx, tool_model, tools)
                        append_records(records_path, [record])
                        return record

                    if run_within_budget(batch, one, spent, cfg.max_usd)[1]:
                        return True
        finally:
            if bridge is not None:
                bridge.__exit__(None, None, None)
            if isinstance(routes, HttpRouteApi):
                routes.close()
    return False


def run_one(arm: Arm, item: EvalTask, ctx: ArmContext, tool_model: ToolModel, tools: dict) -> TaskRecord:  # type: ignore[type-arg]
    if arm is Arm.B0:
        return run_b0(item.bench, ctx)
    if arm is Arm.B1:
        return run_b1(item.bench, ctx)
    if arm is Arm.ATLAS:
        return run_atlas(item.bench, ctx)
    ctx.driver.act(Action(kind=ActionKind.GOTO, value=item.bench.spec.start_path))
    return run_m(arm, item.bench, ctx, tool_model, tools)


def run_benchmark(
    cfg: RunConfig, settings: Settings, model: ModelClient, tool_model: ToolModel, spent: Callable[[], float]
) -> bool:
    """Run the benchmark; True when it stopped early at the spending limit (results are partial)."""
    cfg.out_dir.mkdir(parents=True, exist_ok=True)
    partial = False
    with TestMart(cfg.out_dir, cfg.testmart_port, settings) as mart:
        for seed in cfg.seeds:
            for arm in cfg.arms:
                if partial:
                    break
                site = f"bench-{cfg.run_id}-s{seed}-{arm.value}".lower().replace("_", "-")[:63]
                tasks = build_tasks(site, seed, cfg.iterations * cfg.stream_per_iteration, cfg.heldout)
                partial = run_arm(cfg, arm, seed, site, settings, mart, model, tool_model, tasks, spent)
    records = read_records(cfg.out_dir / "records.jsonl")
    (cfg.out_dir / "summary.json").write_text(
        "[" + ",".join(row.model_dump_json() for row in summarise(records)) + "]", encoding="utf-8"
    )
    write_meta(cfg.out_dir, partial=partial, spent_usd=spent(), max_usd=cfg.max_usd)
    draw_all(cfg.out_dir)
    return partial


def main() -> int:
    cfg = parse_args(sys.argv[1:])
    settings = Settings()
    try:
        key = model_api_key(settings)
        model = build_model_client(settings)
    except ModelUnavailableError as exc:
        sys.stderr.write(f"eval: {exc}; the benchmark needs a model\n")
        return 2
    ledger = ledger_from_settings(settings)
    start = ledger.spent_usd
    tool_model: ToolModel = BudgetedToolModel(
        OpenAIToolModel(key, settings.atlas_model_id, model_base_url(settings))
        if settings.model_provider == "openai"
        else AnthropicToolModel(key, settings.atlas_model_id),
        ledger,
    )
    partial = run_benchmark(cfg, settings, model, tool_model, lambda: ledger.spent_usd - start)
    note = " (partial run: spending limit reached)" if partial else ""
    sys.stdout.write(f"eval: wrote {cfg.out_dir}{note}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
