"""D13.2 key note: a second key from one address still issues, with a note; the first has none."""

from __future__ import annotations

from tests.support.hosted import hosted, hosted_settings


def test_second_key_from_one_address_carries_a_note(fake_store, ns, tmp_path):
    with hosted(fake_store, hosted_settings(tmp_path, ns)) as client:
        first = client.post("/v1/keys", headers={"X-Forwarded-For": "203.0.113.5"})
        second = client.post("/v1/keys", headers={"X-Forwarded-For": "203.0.113.5"})
        other = client.post("/v1/keys", headers={"X-Forwarded-For": "203.0.113.6"})
    assert first.status_code == second.status_code == 201
    assert "note" not in first.json() and "note" not in other.json()
    note = second.json()["note"]
    assert "already created 1 key(s) today" in note and "empty map" in note
    assert "keep using that key" in note
    assert second.json()["api_key"] != first.json()["api_key"]
    assert set(second.json()) == {"api_key", "namespace", "mcp_url", "read_only", "note"}


def test_the_note_counts_and_the_limit_is_unchanged(fake_store, ns, tmp_path):
    with hosted(fake_store, hosted_settings(tmp_path, ns, tenant_keys_per_ip_day=3)) as client:
        ip = {"X-Forwarded-For": "198.51.100.77"}
        bodies = [client.post("/v1/keys", headers=ip) for _ in range(3)]
        refused = client.post("/v1/keys", headers=ip)
    assert [r.status_code for r in bodies] == [201, 201, 201]
    assert "already created 2 key(s)" in bodies[2].json()["note"]
    assert refused.status_code == 429 and refused.json()["code"] == "tenant_limit"
    assert "keep using that key" in refused.json()["message"]
