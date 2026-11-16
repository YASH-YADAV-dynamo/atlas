"""MCP tool contract (SPEC 3c). Targets atlas.contracts.mcp_models only, so it passes
before the MCP lane starts and pins what the lane must register."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from atlas.contracts import mcp_models as m
from atlas.contracts.api_models import ReportRequest

RUNNER = {"atlas_find_skills", "atlas_run", "atlas_act", "atlas_recall"}
PLANNER = {"atlas_find_skills", "atlas_recall", "atlas_plan", "atlas_report"}


def test_tool_set_is_small_and_stable():
    """R-MCP-1: runner mode exposes four tools, planner mode four; six names in total."""
    assert {tool.name for tool in m.tools_for(m.Mode.RUNNER)} == RUNNER
    assert {tool.name for tool in m.tools_for(m.Mode.PLANNER)} == PLANNER
    assert len({tool.name for tool in m.TOOLS}) == len(m.TOOLS) == 6


@pytest.mark.parametrize("tool", m.TOOLS, ids=lambda tool: tool.name)
def test_tool_schemas_are_closed_and_described(tool):
    """R-MCP-1: every tool has a short description and a closed JSON-schema object as input."""
    schema = tool.input_model.model_json_schema()
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert 20 <= len(tool.description) <= 240
    json.dumps(tool.output_model.model_json_schema())


@pytest.mark.parametrize("mode", list(m.Mode))
def test_tool_definitions_fit_their_context_budget(mode):
    """R-MCP-2: the definitions an agent must load stay under the stated budget per mode."""
    total = sum(m.definition_tokens(tool) for tool in m.tools_for(mode))
    assert total <= m.MCP_DEFINITIONS_TOKEN_BUDGET[mode.value]


def test_token_counting_is_the_stated_rule():
    """R-MCP-2: tokens = ceil(characters / 3) of the compact JSON a tool returns."""
    result = m.RunOutput(status=m.RunStatus.DONE, steps_driven=5, model_calls_saved=5, result_summary="ok")
    text = m.tool_text(result)
    assert text == '{"status":"done","steps_driven":5,"model_calls_saved":5,"result_summary":"ok"}'
    assert m.response_tokens(result) == -(-len(text) // 3)
    assert (m.count_tokens(""), m.count_tokens("abc"), m.count_tokens("abcd")) == (0, 1, 2)
    assert m.MCP_RESPONSE_TOKEN_BUDGET == 400


BAD_INPUTS = [
    (m.RunInput, {}),
    (m.RunInput, {"goal_text": "x", "skill_id": "goal:1"}),
    (m.RunInput, {"skill_id": "goal:1'}) DETACH DELETE n //"}),
    (m.RunInput, {"goal_text": "x", "params": {"Bad Slot": "v"}}),
    (m.ActInput, {}),
    (m.ActInput, {"action": 1, "goal_reached": True}),
    (m.ActInput, {"action": m.MAX_CHOICES}),
    (m.ActInput, {"goal_reached": True, "value": "x"}),
    (m.ActInput, {"action": 1, "script": "alert(1)"}),
    (m.RecallInput, {"entity_type": "order id", "key": "1"}),
    (m.FindSkillsInput, {"site": "bad site!", "goal_text": "x"}),
]


@pytest.mark.parametrize(("model", "payload"), BAD_INPUTS)
def test_tool_inputs_reject_malformed_arguments(model, payload):
    """R-MCP-1, R-SEC-2: tool arguments are validated by the contract models."""
    with pytest.raises(ValidationError):
        model.model_validate(payload)


def test_choices_only_travel_with_needs_choice():
    """R-MCP-3: a finished or failed run never carries a page's controls."""
    choice = m.Choice(id=0, role="link", name="Orders")
    assert m.RunOutput(status=m.RunStatus.NEEDS_CHOICE, choices=(choice,)).choices == (choice,)
    with pytest.raises(ValidationError):
        m.RunOutput(status=m.RunStatus.DONE, choices=(choice,))


def test_planner_report_shares_the_route_api_fields():
    """R-MCP-6: atlas_report is the Route API report with snapshot renamed and bookkeeping optional."""
    tool_fields = set(m.ReportToolInput.model_fields) - {"to_snapshot_summary"}
    api_fields = set(ReportRequest.model_fields) - {"to_snapshot"}
    assert tool_fields == api_fields
