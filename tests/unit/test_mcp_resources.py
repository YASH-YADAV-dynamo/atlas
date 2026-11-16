"""MCP resources (SPEC 3c). Targets (lane P):
    atlas.mcp.resources.sites_resource(store, ns) -> SitesResource
    atlas.mcp.resources.changes_resource(store, ns, site) -> ChangesResource
    both stores: list_sites(ns) -> ((site, states, transitions), ...) ordered by site
Both results are already within the response token budget; `server.py` serves them as JSON
text at SITES_URI and SITE_CHANGES_URI.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

pytest.importorskip("atlas.mcp.resources", reason="increment 6: implemented by lane P")

from atlas.contracts import mcp_models as m
from atlas.contracts.types import ChangeEvent
from atlas.core.ids import stable_id
from atlas.mcp.resources import changes_resource, sites_resource
from tests.contract.test_change_store_contract import T0
from tests.support.builders import SITE, click, learn, page


def grow(store, ns, site: str, pages: int) -> None:
    """A chain of `pages` states joined by pages - 1 transitions on `site`."""
    views = [page(f"/{site}/{name}", f"button:next-{name}") for name in "abcdefgh"[:pages]]
    for index in range(pages - 1):
        learn(store, ns, views[index], click(f"next-{'abcdefgh'[index]}"), views[index + 1], site=site)


def test_sites_resource_lists_mapped_sites_with_sizes(fake_store, ns):
    """R-MCP-10: atlas://sites lists every mapped site of the namespace with its state and
    transition counts, ordered by name, and nothing from another namespace."""
    grow(fake_store, ns, "shop.test", 3)
    grow(fake_store, ns, "desk.test", 5)
    grow(fake_store, f"{ns}x", "other.test", 2)
    assert fake_store.list_sites(ns) == (("desk.test", 5, 4), ("shop.test", 3, 2))
    result = sites_resource(fake_store, ns)
    assert result == m.SitesResource(
        sites=(
            m.SiteInfo(site="desk.test", states=5, transitions=4),
            m.SiteInfo(site="shop.test", states=3, transitions=2),
        )
    )
    assert sites_resource(fake_store, "empty-namespace") == m.SitesResource()


def test_changes_resource_is_brief_newest_first_and_within_budget(fake_store, ns):
    """R-MCP-10, R-MCP-2: atlas://site/{site}/changes gives control, page and time per change,
    newest first, capped and inside the token budget even with many long-named changes."""
    if not hasattr(fake_store, "record_change"):
        pytest.skip("wave 3: ChangeStore methods not merged yet")
    a = page("/orders/7", "button:" + "Refund this order now " * 4)
    for index in range(m.MAX_CHANGES + 5):
        name = f"{'Refund this order now ' * 4}{'abcdefghijklmnopqrst'[index]}"
        edge = learn(fake_store, ns, a, click(name), page(f"/done/{'abcdefghijklmnopqrst'[index]}", "link:x"))
        (transition,) = edge.transitions
        fake_store.record_change(
            ns,
            ChangeEvent(
                id=stable_id(ns, "change", transition.id),
                site=SITE,
                transition_id=transition.id,
                from_state=transition.from_state,
                expected_state=transition.to_state,
                action=transition.action,
                first_seen=T0 + timedelta(minutes=index),
                replacement_id=transition.id if index % 2 else None,
                actual_state=transition.to_state if index % 2 else None,
            ),
        )
    result = changes_resource(fake_store, ns, SITE)
    assert m.response_tokens(result) <= m.MCP_RESPONSE_TOKEN_BUDGET
    assert (result.site, result.truncated) == (SITE, True)
    assert 1 <= len(result.changes) <= m.MAX_CHANGES
    times = [change.first_seen for change in result.changes]
    assert times == sorted(times, reverse=True)
    assert times[0] == T0 + timedelta(minutes=m.MAX_CHANGES + 4)
    newest = result.changes[0]
    assert newest.page == "/orders/{}"
    assert newest.control == ("Refund this order now " * 4)[: m.CHOICE_NAME_LENGTH]
    assert newest.replaced is ((m.MAX_CHANGES + 4) % 2 == 1)
    assert changes_resource(fake_store, ns, "other.test") == m.ChangesResource(site="other.test")
