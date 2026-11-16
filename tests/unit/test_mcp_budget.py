"""Response token budget. Targets atlas.mcp.budget.enforce_budget(result: T) -> T (lane M):
returns the result unchanged when it fits; otherwise drops trailing list items and shortens
free text until it fits, and sets `truncated=True`. Scalars are never altered."""

from __future__ import annotations

import pytest

pytest.importorskip("atlas.mcp.budget", reason="MCP wave: implemented by lane M")

from hypothesis import given
from hypothesis import strategies as st

from atlas.contracts import mcp_models as m
from atlas.contracts.types import Action, ActionKind, Locator
from atlas.mcp.budget import enforce_budget

TEXT = st.text(alphabet=st.characters(codec="utf-8", exclude_categories=("Cs",)), max_size=200)
NAMES = st.text(alphabet="abcdefghij XYZ", max_size=m.CHOICE_NAME_LENGTH)
CHOICES = st.lists(NAMES, max_size=m.MAX_CHOICES).map(
    lambda names: tuple(m.Choice(id=i, role="link", name=name) for i, name in enumerate(names))
)
ENTITIES = st.lists(st.builds(m.EntityRef, type=st.just("order_id"), key=TEXT), max_size=m.MAX_ENTITIES).map(
    tuple
)
RUN_OUTPUTS = st.builds(
    m.RunOutput,
    status=st.just(m.RunStatus.NEEDS_CHOICE),
    steps_driven=st.integers(0, 50),
    model_calls_saved=st.integers(0, 50),
    result_summary=TEXT,
    entities_resolved=ENTITIES,
    choices=CHOICES,
)


@given(RUN_OUTPUTS)
def test_any_run_output_is_brought_within_budget(result):
    """R-MCP-2: every response fits 400 tokens; cutting keeps prefixes and never changes scalars."""
    fitted = enforce_budget(result)
    assert m.response_tokens(fitted) <= m.MCP_RESPONSE_TOKEN_BUDGET
    assert (fitted.status, fitted.steps_driven, fitted.model_calls_saved) == (
        result.status,
        result.steps_driven,
        result.model_calls_saved,
    )
    assert fitted.choices == result.choices[: len(fitted.choices)]
    assert fitted.entities_resolved == result.entities_resolved[: len(fitted.entities_resolved)]
    assert result.result_summary.startswith(fitted.result_summary)
    if m.response_tokens(result) <= m.MCP_RESPONSE_TOKEN_BUDGET:
        assert fitted == result
    else:
        assert fitted.truncated is True
        assert fitted != result


def test_long_routes_are_cut_from_the_end():
    """R-MCP-2: route-bearing outputs (recall, plan) are cut the same way."""
    step = m.BriefStep(
        action=Action(
            kind=ActionKind.CLICK, locator=Locator(role="link", name="A fairly long control name " * 3)
        ),
        expect=123456789012345,
        reliability=0.9,
    )
    result = m.RecallOutput(found=True, state_id=42, page="/orders/{}", route=(step,) * m.MAX_BRIEF_STEPS)
    assert m.response_tokens(result) > m.MCP_RESPONSE_TOKEN_BUDGET
    fitted = enforce_budget(result)
    assert m.response_tokens(fitted) <= m.MCP_RESPONSE_TOKEN_BUDGET
    assert (fitted.found, fitted.state_id, fitted.page, fitted.truncated) == (True, 42, "/orders/{}", True)
    assert 1 <= len(fitted.route) < m.MAX_BRIEF_STEPS
