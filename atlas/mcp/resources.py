"""MCP resources (R-MCP-10): read-only views served as JSON text. Both results are already
within the response token budget."""

from __future__ import annotations

from atlas.contracts import mcp_models as m
from atlas.contracts.ports import RouteStore, SiteStore
from atlas.mcp.budget import enforce_budget


def sites_resource(store: SiteStore, ns: str) -> m.SitesResource:
    """`atlas://sites`: every mapped site with its state and transition counts."""
    rows = store.list_sites(ns)
    sites = tuple(m.SiteInfo(site=site, states=states, transitions=edges) for site, states, edges in rows)
    return enforce_budget(m.SitesResource(sites=sites[: m.MAX_SITES], truncated=len(sites) > m.MAX_SITES))


def changes_resource(store: RouteStore, ns: str, site: str) -> m.ChangesResource:
    """`atlas://site/{site}/changes`: the newest flow changes, control name and page only."""
    events = store.list_changes(ns, site)
    briefs: list[m.ChangeBrief] = []
    for event in events[: m.MAX_CHANGES]:
        origin = store.get_state(ns, event.from_state)
        control = event.action.locator.name if event.action.locator is not None else event.action.kind.value
        briefs.append(
            m.ChangeBrief(
                control=control[: m.CHOICE_NAME_LENGTH],
                page=origin.url_tmpl[:200] if origin is not None else "",
                first_seen=event.first_seen,
                replaced=event.replacement_id is not None,
            )
        )
    return enforce_budget(
        m.ChangesResource(site=site, changes=tuple(briefs), truncated=len(events) > m.MAX_CHANGES)
    )
