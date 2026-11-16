"""Hosted MCP: atlas_plan on a private key stops recording new pages at TENANT_MAX_PLAN_STATES;
the demo key and other tenants are unaffected."""

from __future__ import annotations

from tests.support.builders import page
from tests.support.hosted import (
    ADMIN,
    DEMO,
    RawMcp,
    hosted,
    hosted_settings,
    new_key,
    plan_args,
    snapshot_json,
)

NAMES = ["alpha", "bravo", "charlie", "delta"]


def plan_page(mcp: RawMcp, n: int):
    args = plan_args()
    args["snapshot_summary"] = snapshot_json(page(f"/{NAMES[n]}", f"link:{NAMES[n]}"))
    return mcp.call("atlas_plan", args)


def test_private_key_plan_is_capped_but_known_pages_and_others_are_not(fake_store, ns, tmp_path):
    settings = hosted_settings(tmp_path, ns, tenant_max_plan_states=3)
    with hosted(fake_store, settings) as client:
        a = RawMcp(client, new_key(client, "10.0.0.1")).initialize()
        b = RawMcp(client, new_key(client, "10.0.0.2")).initialize()
        for n in range(3):
            error, out = plan_page(a, n)
            assert not error, out
        error, out = plan_page(a, 3)
        assert error and "map size limit reached for this key" in str(out)
        error, out = plan_page(a, 0)  # already known: still allowed
        assert not error, out
        error, out = plan_page(b, 3)  # another tenant
        assert not error, out


def test_demo_key_is_not_counted(fake_store, ns, tmp_path):
    settings = hosted_settings(tmp_path, ns, tenant_max_plan_states=1)
    with hosted(fake_store, settings) as client:
        assert not RawMcp(client, ADMIN).initialize().call("atlas_plan", plan_args())[0]
        demo = RawMcp(client, DEMO).initialize()
        for n in range(3):
            error, out = plan_page(demo, n)
            assert not error, out
