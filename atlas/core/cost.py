"""Edge cost (SPEC 3): unreliable and slow transitions are expensive."""

from __future__ import annotations

import math


def reliability(ok: int, fail: int) -> float:
    return (ok + 1) / (ok + fail + 2)


def edge_cost(ok: int, fail: int, ms: float) -> float:
    return -math.log(reliability(ok, fail)) + ms / 10000
