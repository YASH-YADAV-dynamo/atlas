"""REST: oversized snapshots are truncated, never rejected (A1-A3, A9); blocked pages carry `blocked`."""

from __future__ import annotations

from atlas.contracts.api_models import LOCATE_PATH, PLAN_PATH, REPORT_PATH
from tests.support.builders import GOAL, SITE, click, page
from tests.support.scenarios import client_for, post, report_body


def big_snapshot(url="shop.test/Big") -> dict:
    return {
        "url": url,
        "title": "t" * 500,
        "text": "x" * 9000,
        "elements": [{"role": "link", "name": "n" * 300 + str(i)} for i in range(450)]
        + [{"role": "heading", "name": "h"}],
    }


def test_locate_plan_report_accept_oversized_snapshots(fake_store, ns):
    client = client_for(fake_store, ns)
    located = post(client, LOCATE_PATH, {"site": SITE, "snapshot": big_snapshot()})
    assert located["state_id"] and located["known"] is False
    again = post(client, LOCATE_PATH, {"site": SITE, "snapshot": big_snapshot("https://SHOP.test/Big")})
    assert again["state_id"] == located["state_id"] and again["known"] is True
    planned = post(
        client,
        PLAN_PATH,
        {"site": SITE, "state_id": located["state_id"], "goal_text": GOAL, "snapshot": big_snapshot()},
    )
    assert planned["needs_model"] is True
    body = report_body(located["state_id"], click("Go"), page("/next", "button:A"))
    body["to_snapshot"] = big_snapshot("shop.test/next")
    reported = post(client, REPORT_PATH, body)
    assert reported["known"] is False and "blocked" not in reported


def test_blocked_page_is_flagged_everywhere(fake_store, ns):
    client = client_for(fake_store, ns)
    wall = {"url": "https://shop.test/sorry", "title": "Sorry", "elements": [], "status": 429}
    located = post(client, LOCATE_PATH, {"site": SITE, "snapshot": wall})
    assert located["blocked"] and located["known"] is False
    planned = post(client, PLAN_PATH, {"site": SITE, "state_id": 5, "goal_text": GOAL, "snapshot": wall})
    assert planned["needs_model"] is True and planned["blocked"]
    body = report_body(5, click("Go"), page("/x"), goal_text=GOAL, goal_reached=True)
    body["to_snapshot"] = wall
    reported = post(client, REPORT_PATH, body)
    assert reported["blocked"]
    assert len(fake_store.get_map(ns, SITE).states) == 0
