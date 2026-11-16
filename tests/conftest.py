"""Shared fixtures. Real-HydraDB fixtures skip cleanly when HYDRADB_URL is unset."""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import pytest

from atlas.contracts.ports import GraphStore


@pytest.fixture
def ns() -> str:
    """A fresh namespace per test, so tests sharing one HydraDB never see each other."""
    return f"t{uuid.uuid4().hex[:16]}"


@pytest.fixture
def fake_store() -> GraphStore:
    from atlas.adapters.hydradb.fake import InMemoryGraphStore

    return InMemoryGraphStore()


@pytest.fixture
def hydra_store() -> Iterator[GraphStore]:
    url = os.environ.get("HYDRADB_URL")
    if not url:
        pytest.skip("HYDRADB_URL is not set; real-HydraDB tests skipped")
    from atlas.adapters.hydradb.store import open_store

    store = open_store(url, os.environ.get("HYDRADB_TOKEN", ""), os.environ.get("HYDRADB_GRAPH", "default"))
    try:
        yield store
    finally:
        store.close()


@pytest.fixture(params=["fake", "hydradb"])
def any_store(request: pytest.FixtureRequest) -> GraphStore:
    """The contract-suite store: the in-memory fake and the real adapter, same tests."""
    name = "fake_store" if request.param == "fake" else "hydra_store"
    return request.getfixturevalue(name)
