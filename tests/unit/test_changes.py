"""Flow-change detection (SPEC 5.5) through the Route API on the in-memory store.

Targets (wave 3):
    atlas.core.changes.is_flow_change(transition: Transition, recent: Sequence[bool]) -> bool
        True when transition.ok >= 5 and the two newest outcomes are both failures.
    atlas.core.changes.note_report(store: ChangeStore, clock: Clock, ns: str,
                                   result: ReportResult) -> ChangeEvent | None
        called by the API after every report; records and returns a change when one is detected.
    GET /v1/changes?site=  ->  ChangesResponse
    the ChangeStore port on both stores (recent_outcomes, record_change, list_changes)
"""

from __future__ import annotations

import pytest

pytest.importorskip("atlas.core.changes", reason="wave 3: implemented by lane R")

from hypothesis import given
from hypothesis import strategies as st

from atlas.contracts.api_models import CHANGES_PATH, LOCATE_PATH, REPORT_PATH
from atlas.contracts.types import Transition
from atlas.core.changes import is_flow_change
from tests.support.builders import SITE, TickClock, click, page
from tests.support.scenarios import client_for, post, report_body

A = page("/a", "button:go")
B = page("/b", "button:back")
C = page("/c", "button:elsewhere")


class Flow:
    """Reports on one edge A --go--> B through the API, with a ticking clock."""

    def __init__(self, store, ns) -> None:
        self.client = client_for(store, ns, clock=TickClock())
        self.a = self.locate(A)
        self.b = self.locate(B)

    def locate(self, snapshot) -> int:
        return post(self.client, LOCATE_PATH, {"site": SITE, "snapshot": snapshot.model_dump(mode="json")})[
            "state_id"
        ]

    def ok(self, times: int = 1) -> None:
        for _ in range(times):
            post(self.client, REPORT_PATH, report_body(self.a, click("go"), B))

    def dead(self, times: int = 1) -> None:
        """The control did not respond: the action failed and the page is unchanged."""
        for _ in range(times):
            post(self.client, REPORT_PATH, report_body(self.a, click("go"), A, ok=False, expect_state=self.b))

    def lands_on(self, snapshot, times: int = 1) -> None:
        """The action worked but led somewhere other than the map expected."""
        for _ in range(times):
            post(self.client, REPORT_PATH, report_body(self.a, click("go"), snapshot, expect_state=self.b))

    def changes(self, site: str = SITE) -> list[dict]:
        response = self.client.get(CHANGES_PATH, params={"site": site})
        assert response.status_code == 200, response.text
        assert response.json()["site"] == site
        return response.json()["changes"]


def test_strong_edge_failing_twice_in_a_row_is_a_change(fake_store, ns):
    """R-CHG-1: ok >= 5 then two consecutive failures emit exactly one change event."""
    flow = Flow(fake_store, ns)
    flow.ok(5)
    flow.dead()
    assert flow.changes() == []
    flow.dead()
    (change,) = flow.changes()
    assert (change["from_state"], change["expected_state"]) == (flow.a, flow.b)
    assert change["action"]["locator"]["name"] == "go"
    assert change["site"] == SITE
    assert (change["actual_state"], change["replacement_id"]) == (None, None)
    flow.dead(3)
    assert flow.changes() == [change]


def test_a_success_between_failures_is_not_a_change(fake_store, ns):
    """R-CHG-1: failures must be consecutive; a flaky edge is penalised but not announced."""
    flow = Flow(fake_store, ns)
    flow.ok(5)
    for _ in range(3):
        flow.dead()
        flow.ok()
    assert flow.changes() == []


def test_a_weak_edge_failing_is_not_a_change(fake_store, ns):
    """R-CHG-1: an edge without a strong record (ok < 5) never raises a change event."""
    flow = Flow(fake_store, ns)
    flow.ok(4)
    flow.dead(4)
    assert flow.changes() == []


def test_a_moved_destination_names_its_replacement(fake_store, ns):
    """R-CHG-2: when the action now leads elsewhere, the event carries the new destination
    and the transition that replaces the old one."""
    flow = Flow(fake_store, ns)
    flow.ok(6)
    flow.lands_on(C, times=2)
    (change,) = flow.changes()
    c_id = flow.locate(C)
    transitions = flow.client.get(f"/v1/map/{SITE}").json()["transitions"]
    (replacement,) = [t for t in transitions if t["to_state"] == c_id]
    (old,) = [t for t in transitions if t["to_state"] == flow.b]
    assert change["transition_id"] == old["id"]
    assert (change["actual_state"], change["replacement_id"]) == (c_id, replacement["id"])
    assert (old["ok"], old["fail"], replacement["ok"]) == (6, 2, 2)


def test_changes_are_scoped_validated_and_protected(fake_store, ns):
    """R-API-8: changes are per site; the site is validated; the bearer key applies."""
    flow = Flow(fake_store, ns)
    flow.ok(5)
    flow.dead(2)
    assert len(flow.changes()) == 1
    assert flow.changes("other.test") == []
    assert flow.client.get(CHANGES_PATH, params={"site": "x'}) DETACH DELETE n //"}).status_code == 422
    assert flow.client.get(CHANGES_PATH).status_code == 422
    locked = client_for(fake_store, ns, api_key="test-key-not-a-secret")
    assert locked.get(CHANGES_PATH, params={"site": SITE}).status_code == 401


@given(st.integers(0, 50), st.integers(0, 50), st.lists(st.booleans(), max_size=6))
def test_detection_rule(ok, fail, recent):
    """R-CHG-1: the rule is exactly 'ok >= 5 and the two newest outcomes failed'."""
    transition = Transition(
        id=1, site=SITE, from_state=1, to_state=2, action=click("go"), ok=ok, fail=fail, cost=1.0
    )
    expected = ok >= 5 and len(recent) >= 2 and not recent[0] and not recent[1]
    assert is_flow_change(transition, recent) is expected
