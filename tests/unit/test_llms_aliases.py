"""Misspelled agent-guide paths redirect to /llms.txt and /llms-full.txt."""

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def client():
    import os

    os.environ.setdefault("ATLAS_STORE", "memory")
    from atlas.api.main import app

    return TestClient(app)


@pytest.mark.parametrize(
    ("path", "target"),
    [("/llm.txt", "/llms.txt"), ("/llms.text", "/llms.txt"), ("/llm-full.txt", "/llms-full.txt")],
)
def test_alias_redirects(client, path, target):
    r = client.get(path, follow_redirects=False)
    assert r.status_code == 301 and r.headers["location"] == target


def test_real_file_still_served(client):
    assert client.get("/llms.txt").status_code == 200
