"""D13.4: what cannot be corrected still fails, and the message names the expected shape (<= 300 chars)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from atlas.mcp.server import _validation_message  # pyright: ignore[reportPrivateUsage]
from tests.support.mcp_call import SPECS, call_tool
from tests.unit.test_mcp_plan_replay import tools  # noqa: F401

BAD = {
    "atlas_plan": {"site": "s"},
    "atlas_report": {"site": "s", "steps": "nope"},
    "atlas_find_skills": {"site": 5},
    "atlas_recall": {},
}


@pytest.mark.parametrize("name", sorted(BAD))
def test_uncorrectable_input_names_the_expected_shape(tools, name):  # noqa: F811
    with pytest.raises(ValidationError) as caught:
        call_tool(tools, name, BAD[name])
    message = _validation_message(caught.value, name)
    assert name in message and "Expected:" in message and len(message) <= 300


def test_a_long_validation_error_is_cut_before_the_hint():
    with pytest.raises(ValidationError) as caught:
        SPECS["atlas_report"].input_model.model_validate({"site": "x" * 900, "plan_id": "a" * 12})
    message = _validation_message(caught.value, "atlas_report")
    assert len(message) <= 300 and message.endswith("{site, plan_id, done?}")


def test_no_tool_name_gives_the_plain_message():
    with pytest.raises(ValidationError) as caught:
        SPECS["atlas_recall"].input_model.model_validate({})
    assert "Expected" not in _validation_message(caught.value, None)
