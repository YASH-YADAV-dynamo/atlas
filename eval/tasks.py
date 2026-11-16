"""Benchmark tasks on top of `testmart.tasks`: an experience stream and held-out compositions.

Held-out tasks use the same templates but start from pages the stream never starts on, so the route
has to be composed by joining the learned paths.
"""

from __future__ import annotations

from dataclasses import dataclass

from atlas.contracts.bench_models import BenchTask
from atlas.contracts.types import TaskSpec
from testmart.tasks import Task, generate_tasks

STREAM_START = "/"
HELDOUT_STARTS = ("/customers", "/products", "/settings/general", "/orders")


@dataclass(frozen=True)
class EvalTask:
    bench: BenchTask
    source: Task

    def check(self, db_path: str, answer: str) -> bool:
        return self.source.check(db_path, answer)


def _wrap(task: Task, site: str, start_path: str) -> EvalTask:
    spec = TaskSpec(site=site, goal_text=task.text, params=task.params, start_path=start_path)
    return EvalTask(BenchTask(template_id=task.template_id, spec=spec, needs_answer=task.needs_answer), task)


def build_tasks(site: str, seed: int, stream: int, heldout: int) -> tuple[list[EvalTask], list[EvalTask]]:
    """`stream` tasks starting at the dashboard, then `heldout` tasks starting elsewhere; all params
    come from the TestMart DB seeded with `seed` (reset TestMart with the same seed)."""
    tasks = generate_tasks(stream + heldout, seed)
    experience = [_wrap(t, site, STREAM_START) for t in tasks[:stream]]
    held = [_wrap(t, site, HELDOUT_STARTS[i % len(HELDOUT_STARTS)]) for i, t in enumerate(tasks[stream:])]
    return experience, held
