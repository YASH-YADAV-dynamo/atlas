"""Summary table and pass-bar check (ship plan section 8, bars P1 to P5) from a live records.jsonl.

python -m eval.live.summary results/live-2026-11-16/records.jsonl
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from statistics import median
from typing import Any

from eval.live.common import out

Row = dict[str, Any]
ORDER = ("first", "new_subject", "repeat", "reworded")


def load(path: Path) -> list[Row]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def table(rows: list[Row]) -> list[str]:
    groups: dict[tuple[str, str, str], list[Row]] = defaultdict(list)
    for row in rows:
        groups[(row["suite"], row["scenario"], row["arm"])].append(row)
    lines = [
        "| Suite | Scenario | Arm | Runs | Success | Model calls (total) | Model calls (median) "
        "| Turns (median) | Browser steps (median) | Wall s (median) | $ (total) |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for suite, scenario, arm in sorted(groups, key=lambda k: (k[0], ORDER.index(k[1]), k[2] != "baseline")):
        g = groups[(suite, scenario, arm)]
        lines.append(
            f"| {suite} | {scenario} | {arm} | {len(g)} | {sum(r['success'] for r in g)}/{len(g)} "
            f"| {sum(r['model_calls'] for r in g)} | {median(r['model_calls'] for r in g):g} "
            f"| {median(r['turns'] for r in g):g} | {median(r['browser_steps'] for r in g):g} "
            f"| {median(r['wall_s'] for r in g):.1f} | {sum(r['usd'] for r in g):.4f} |"
        )
    return lines


def pick(rows: list[Row], suite: str, arm: str, scenarios: tuple[str, ...]) -> list[Row]:
    return [r for r in rows if r["suite"] == suite and r["arm"] == arm and r["scenario"] in scenarios]


def bars(rows: list[Row]) -> list[str]:
    lines: list[str] = []

    def bar(name: str, ok: bool | None, detail: str) -> None:
        verdict = "not measured" if ok is None else ("PASS" if ok else "FAIL")
        lines.append(f"| {name} | {verdict} | {detail} |")

    known = ("new_subject", "repeat")
    a_atlas = pick(rows, "A", "atlas", known)
    if a_atlas:
        by: dict[str, list[Row]] = defaultdict(list)
        for r in a_atlas:
            by[f"ctx{r['context']}"].append(r)
            by[r["process"]].append(r)
        shares = {k: sum(r["model_calls"] == 0 for r in v) / len(v) for k, v in sorted(by.items())}
        detail = ", ".join(f"{k} {v:.0%}" for k, v in shares.items())
        bar("P1 A: 0 model calls on >= 90% of runs after the first, every context and process",
            min(shares.values()) >= 0.9, detail)  # fmt: skip
        a_base = pick(rows, "A", "baseline", known)
        ma, mb = median(r["wall_s"] for r in a_atlas), median(r["wall_s"] for r in a_base)
        bar(
            "P2 A: median wall time of known runs not above baseline",
            ma <= mb,
            f"atlas {ma:.1f}s vs {mb:.1f}s",
        )
    b_rep, b_rep0 = pick(rows, "B", "atlas", ("repeat",)), pick(rows, "B", "baseline", ("repeat",))
    if b_rep and b_rep0:
        ta, tb = median(r["turns"] for r in b_rep), median(r["turns"] for r in b_rep0)
        wa, wb = median(r["wall_s"] for r in b_rep), median(r["wall_s"] for r in b_rep0)
        bar("P3 B exact repeat: >= 1 turn fewer and wall time not above baseline", ta <= tb - 1 and wa <= wb,
            f"turns {ta:g} vs {tb:g}, wall {wa:.1f}s vs {wb:.1f}s")  # fmt: skip
    b_new, b_new0 = pick(rows, "B", "atlas", ("new_subject",)), pick(rows, "B", "baseline", ("new_subject",))
    b_miss, b_miss0 = pick(rows, "B", "atlas", ("first",)), pick(rows, "B", "baseline", ("first",))
    if b_new and b_miss:
        tn, tn0 = median(r["turns"] for r in b_new), median(r["turns"] for r in b_new0)
        tm, tm0 = median(r["turns"] for r in b_miss), median(r["turns"] for r in b_miss0)
        bar("P4 B: new subject turns not above baseline; miss at most baseline + 2",
            tn <= tn0 and tm <= tm0 + 2, f"new {tn:g} vs {tn0:g}; miss {tm:g} vs {tm0:g}")  # fmt: skip
    for suite in sorted({r["suite"] for r in rows}):
        sa = sum(r["success"] for r in rows if r["suite"] == suite and r["arm"] == "atlas")
        sb = sum(r["success"] for r in rows if r["suite"] == suite and r["arm"] == "baseline")
        n = sum(1 for r in rows if r["suite"] == suite and r["arm"] == "atlas")
        bar(
            f"P5 {suite}: success at least baseline minus one",
            sa >= sb - 1,
            f"atlas {sa}/{n} vs baseline {sb}/{n}",
        )
    return ["| Bar | Verdict | Measured |", "|---|---|---|", *lines]


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("records", type=Path)
    a = p.parse_args()
    rows = load(a.records)
    spend = sum(r["usd"] for r in rows)
    text = "\n".join([*table(rows), "", *bars(rows), "", f"Model spend in these runs: ${spend:.4f}"])
    out(text)
    (a.records.parent / "summary.md").write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
