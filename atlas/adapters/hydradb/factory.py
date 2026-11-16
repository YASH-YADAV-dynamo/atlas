"""One rule for choosing the map store, shared by the API and the MCP server (R-OPS-2).

`settings.atlas_store` is "hydradb" (default) or "memory" (tests and offline demos, only when
set explicitly). HydraDB is never replaced silently: if it is selected but misconfigured or
unreachable, startup fails with a clear error.
"""

from __future__ import annotations

from atlas.contracts.errors import StoreUnavailableError
from atlas.contracts.ports import MapStore
from atlas.settings import Settings


def open_configured_store(settings: Settings) -> MapStore:
    choice = settings.atlas_store.strip().lower()
    if choice == "memory":
        from atlas.adapters.hydradb.fake import InMemoryGraphStore

        return InMemoryGraphStore()
    if choice != "hydradb":
        raise StoreUnavailableError(f"ATLAS_STORE must be 'hydradb' or 'memory', got {settings.atlas_store!r}")
    if not settings.hydradb_url:
        raise StoreUnavailableError("ATLAS_STORE=hydradb but HYDRADB_URL is not set")
    from atlas.adapters.hydradb.store import open_store

    store = open_store(settings.hydradb_url, settings.hydradb_token.get_secret_value(), settings.hydradb_graph)
    try:
        store.verify()
    except StoreUnavailableError as exc:
        store.close()
        raise StoreUnavailableError(
            f"HydraDB is selected (ATLAS_STORE=hydradb) but unreachable at {settings.hydradb_url}; "
            "check HYDRADB_URL, HYDRADB_TOKEN and HYDRADB_GRAPH, or set ATLAS_STORE=memory explicitly"
        ) from exc
    return store
