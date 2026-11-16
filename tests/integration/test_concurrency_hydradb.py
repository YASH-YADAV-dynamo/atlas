"""Many agents, one map, on real HydraDB (wave 2). Skipped cleanly when HYDRADB_URL is unset.

The store must be safe to call from many threads at once (one driver, a session per call).
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from atlas.contracts.api_models import LOCATE_PATH, PLAN_PATH, REPORT_PATH
from tests.support.builders import GOAL, SITE, click, page
from tests.support.scenarios import client_for, post, report_body

pytestmark = pytest.mark.integration
A = page("/a", "button:go")
B = page("/b", "button:finish")
C = page("/c", "button:done")
REPORTERS = 20


def edge_counts(client) -> dict[str, tuple[int, int]]:
    transitions = client.get(f"/v1/map/{SITE}").json()["transitions"]
    return {t["action"]["locator"]["name"]: (t["ok"], t["fail"]) for t in transitions}


def test_concurrent_reporters_give_exact_totals(hydra_store, ns):
    """R-LEARN-6: 20 agents reporting the same edge at once, a mix of successes and failures,
    each also retrying its own report, end with exactly 14 successes and 6 failures."""
    client = client_for(hydra_store, ns)
    a = post(client, LOCATE_PATH, {"site": SITE, "snapshot": A.model_dump(mode="json")})["state_id"]
    b = post(client, LOCATE_PATH, {"site": SITE, "snapshot": B.model_dump(mode="json")})["state_id"]
    good = [report_body(a, click("go"), B) for _ in range(14)]
    bad = [report_body(a, click("go"), A, ok=False, expect_state=b) for _ in range(6)]

    def send(body: dict) -> list[tuple[int, bool]]:
        replies = [client.post(REPORT_PATH, json=body) for _ in range(2)]
        return [(r.status_code, r.json().get("duplicate")) for r in replies]

    with ThreadPoolExecutor(max_workers=REPORTERS) as pool:
        outcomes = list(pool.map(send, [*good, *bad]))
    assert all(codes == [(200, False), (200, True)] for codes in outcomes)
    assert edge_counts(client) == {"go": (14, 6)}

    for body in (*good, *bad):
        assert post(client, REPORT_PATH, body)["duplicate"] is True
    assert edge_counts(client) == {"go": (14, 6)}


def test_planners_read_while_reporters_write(hydra_store, ns):
    """R-LEARN-6, R-STORE-2: planning concurrently with writes never errors and, once the
    writers finish, the stored cost matches the spec formula for the exact counts."""
    from atlas.core.cost import edge_cost

    client = client_for(hydra_store, ns)
    a = post(client, LOCATE_PATH, {"site": SITE, "snapshot": A.model_dump(mode="json")})["state_id"]
    b = post(client, REPORT_PATH, report_body(a, click("go"), B, ms=100))["state_id"]
    post(client, REPORT_PATH, report_body(b, click("finish"), C, ms=100, goal_text=GOAL, goal_reached=True))
    plan_body = {"site": SITE, "state_id": a, "goal_text": GOAL, "params": {}}

    def write(_: int) -> int:
        return client.post(REPORT_PATH, json=report_body(a, click("go"), B, ms=100)).status_code

    def read(_: int) -> tuple[int, int]:
        response = client.post(PLAN_PATH, json=plan_body)
        return response.status_code, len(response.json().get("route", []))

    with ThreadPoolExecutor(max_workers=REPORTERS) as pool:
        writes = pool.map(write, range(REPORTERS))
        reads = pool.map(read, range(REPORTERS))
        assert set(writes) == {200}
        assert set(reads) == {(200, 2)}

    assert edge_counts(client) == {"go": (REPORTERS + 1, 0), "finish": (1, 0)}
    go = next(
        t
        for t in client.get(f"/v1/map/{SITE}").json()["transitions"]
        if t["action"]["locator"]["name"] == "go"
    )
    assert go["cost"] == pytest.approx(edge_cost(REPORTERS + 1, 0, 100))
