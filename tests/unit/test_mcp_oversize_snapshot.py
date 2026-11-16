"""MCP atlas_plan / atlas_report: oversized snapshots are truncated, never rejected; blocked pages flagged."""

from __future__ import annotations

from tests.support.builders import SITE
from tests.support.hosted import GOAL, RawMcp, hosted, hosted_settings, new_key, open_order, report_args


def big(url="shop.test/Big") -> dict:
    return {
        "url": url,
        "title": "t" * 500,
        "text": "x" * 9000,
        "elements": [{"role": "link", "name": "n" * 300 + str(i)} for i in range(450)],
    }


def plan(mcp: RawMcp, snapshot: dict):
    return mcp.call(
        "atlas_plan",
        {"site": SITE, "snapshot_summary": snapshot, "goal_text": GOAL, "params": {"order_id": "1042"}},
    )


def test_plan_and_report_accept_oversized_snapshots(fake_store, ns, tmp_path):
    with hosted(fake_store, hosted_settings(tmp_path, ns)) as client:
        mcp = RawMcp(client, new_key(client, "10.0.0.1")).initialize()
        error, out = plan(mcp, big())
        assert not error, out
        args = report_args(out["state_id"])
        args["to_snapshot_summary"] = big("shop.test/Next")
        error, rep = mcp.call("atlas_report", args)
        assert not error, rep
        assert rep["known"] is False and "blocked" not in rep


def test_blocked_page_needs_the_model_and_is_not_learned(fake_store, ns, tmp_path):
    with hosted(fake_store, hosted_settings(tmp_path, ns)) as client:
        mcp = RawMcp(client, new_key(client, "10.0.0.1")).initialize()
        wall = {"url": "https://shop.test/captcha", "title": "Verify", "elements": []}
        error, out = plan(mcp, wall)
        assert not error, out
        assert out["needs_model"] is True and out["blocked"]
        args = report_args(out["state_id"])
        args["to_snapshot_summary"] = {**wall, "status": 403}
        args["action"] = open_order().model_dump(mode="json", exclude_none=True)
        error, rep = mcp.call("atlas_report", args)
        assert not error, rep
        assert rep["blocked"]
        assert len(fake_store.get_map(ns, SITE).states) == 0
