"""Cost guard pieces that need no new code path: the budget error's HTTP mapping and the
prompt's stable prefix (system instructions and tool definition first, task content last)."""

from __future__ import annotations

import json

import httpx

from atlas.adapters.model.client import SYSTEM, build_prompt, decision_schema
from atlas.adapters.model.openai_compat import OpenAICompatModelClient
from atlas.contracts import errors
from tests.support.builders import page


def test_budget_error_is_a_mapped_api_error():
    """R-COST-2: the typed error maps to 429 with its own code, never a 500."""
    error = errors.BudgetExceededError("model budget exhausted")
    assert issubclass(errors.BudgetExceededError, errors.AtlasError)
    assert (errors.http_status_for(error), error.code) == (429, "budget_exceeded")


def test_prompt_has_a_stable_prefix():
    """R-COST-5: the system message and the tool definition are identical for every call and
    carry no task data; in the user message the goal and parameters come before the page."""
    sent: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        call = {"function": {"name": "next_step", "arguments": json.dumps({"action": None, "done": True})}}
        body = {
            "choices": [{"message": {"tool_calls": [call]}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 1},
        }
        return httpx.Response(200, json=body)

    client = OpenAICompatModelClient(
        "k", "m", "https://model.example", transport=httpx.MockTransport(handler)
    )
    client.next_action(
        "Refund order 1042", {"order_id": "1042"}, page("/orders", "button:Search", rows=3), ()
    )
    client.next_action(
        "Set product SKU-9 price to 5.00", {"sku": "SKU-9"}, page("/products/9", "button:Save"), ("ok: x",)
    )
    first, second = sent
    assert first["messages"][0] == second["messages"][0] == {"role": "system", "content": SYSTEM}
    assert first["tools"] == second["tools"]
    assert first["messages"][1]["role"] == "user" and len(first["messages"]) == 2
    for secret in ("1042", "SKU-9", "Refund", "/orders"):
        assert secret not in SYSTEM and secret not in json.dumps(first["tools"])
    assert json.dumps(decision_schema(), sort_keys=True) == json.dumps(decision_schema(), sort_keys=True)

    one = build_prompt("Refund order 1042", {"order_id": "1042"}, page("/orders", "button:Search"), ())
    two = build_prompt(
        "Refund order 1042", {"order_id": "1042"}, page("/orders/7", "link:Refund order"), ("ok: y",)
    )
    shared = next(i for i, (a, b) in enumerate(zip(one, two, strict=False)) if a != b)
    assert "Refund order 1042" in one[:shared] and "order_id" in one[:shared]
