"""How the hosted (planner-mode) tools read to an agent: descriptions say when to call, what
comes back and what it costs; compact schemas stay within the definitions budget and accept
exactly the calls the contract models accept; errors tell the agent what to do next."""

from __future__ import annotations

import json

import jsonschema
import pytest

from atlas.contracts import mcp_models as m
from atlas.mcp.catalog import DESCRIPTIONS, listed_description, listed_schema, listed_total
from tests.support.builders import SITE
from tests.support.hosted import ADMIN, DEMO, RawMcp, hosted, hosted_settings, plan_args, report_args

PLANNER = m.tools_for(m.Mode.PLANNER)


def test_listed_definitions_fit_the_budget_of_each_mode():
    for mode in m.Mode:
        assert listed_total(mode) <= m.MCP_DEFINITIONS_TOKEN_BUDGET[mode.value]


@pytest.mark.parametrize("spec", PLANNER, ids=lambda spec: spec.name)
def test_planner_descriptions_say_when_what_and_cost(spec):
    text = listed_description(spec, m.Mode.PLANNER)
    assert spec.name in DESCRIPTIONS
    assert text.startswith("Call ")
    assert "Returns" in text
    assert "tokens" in text


@pytest.mark.parametrize("spec", PLANNER, ids=lambda spec: spec.name)
def test_compact_schema_has_no_titles_and_a_hint_per_required_field(spec):
    schema = listed_schema(spec, m.Mode.PLANNER)
    text = json.dumps(schema, separators=(",", ":"))
    assert '"title":' not in text.replace('"title":{', "")
    jsonschema.Draft202012Validator.check_schema(schema)
    for name in schema["required"]:
        assert schema["properties"][name].get("description"), name


def test_compact_schemas_accept_the_calls_the_contract_accepts():
    calls = {
        "atlas_plan": plan_args(),
        "atlas_report": {**report_args(7, event_id="evt-12345678"), "run_id": "run-12345678"},
        "atlas_recall": {"entity_type": "order_id", "key": "1042"},
        "atlas_find_skills": {"site": SITE, "goal_text": "open order"},
    }
    for spec in PLANNER:
        args = calls[spec.name]
        spec.input_model.model_validate(args)
        jsonschema.validate(args, listed_schema(spec, m.Mode.PLANNER))
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate({**args, "surprise": 1}, listed_schema(spec, m.Mode.PLANNER))


def test_runner_mode_lists_the_contract_unchanged():
    for spec in m.tools_for(m.Mode.RUNNER):
        assert listed_description(spec, m.Mode.RUNNER) == spec.description
        assert listed_schema(spec, m.Mode.RUNNER) == spec.input_model.model_json_schema()


def test_tools_list_over_http_serves_the_catalog(fake_store, ns, tmp_path):
    with hosted(fake_store, hosted_settings(tmp_path, ns)) as client:
        demo = RawMcp(client, DEMO).initialize()
        status, reply = demo.request("tools/list", {})
        assert status == 200 and reply is not None
        listed = {tool["name"]: tool for tool in reply["result"]["tools"]}
        for spec in PLANNER:
            assert listed[spec.name]["description"] == DESCRIPTIONS[spec.name]
            assert listed[spec.name]["inputSchema"] == listed_schema(spec, m.Mode.PLANNER)


def test_read_only_key_naming_an_unmapped_site_is_told_what_exists(fake_store, ns, tmp_path):
    with hosted(fake_store, hosted_settings(tmp_path, ns)) as client:
        admin = RawMcp(client, ADMIN).initialize()
        error, planned = admin.call("atlas_plan", plan_args())
        assert not error
        assert not admin.call("atlas_report", report_args(planned["state_id"]))[0]
        demo = RawMcp(client, DEMO).initialize()

        error, message = demo.call("atlas_find_skills", {"site": "nowhere", "goal_text": "open order"})
        assert error and "unknown site 'nowhere'" in message and SITE in message
        assert "POST /v1/keys" in message
        error, message = demo.call("atlas_plan", {**plan_args(), "site": "nowhere"})
        assert error and "unknown site" in message
        # A known site with nothing matching is an empty answer, not an error.
        error, skills = demo.call("atlas_find_skills", {"site": SITE, "goal_text": "zzz"})
        assert not error and not skills.get("skills")


def test_writable_key_starts_any_site_empty_without_error(fake_store, ns, tmp_path):
    with hosted(fake_store, hosted_settings(tmp_path, ns)) as client:
        admin = RawMcp(client, ADMIN).initialize()
        error, skills = admin.call("atlas_find_skills", {"site": "fresh", "goal_text": "open order"})
        assert not error and not skills.get("skills")


def test_unknown_tool_lists_the_available_ones(fake_store, ns, tmp_path):
    with hosted(fake_store, hosted_settings(tmp_path, ns)) as client:
        demo = RawMcp(client, DEMO).initialize()
        error, message = demo.call("atlas_locate", {})
        assert error and "atlas_plan" in message
