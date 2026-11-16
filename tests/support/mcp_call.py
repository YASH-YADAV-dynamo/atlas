"""Run one MCP tool call the way `atlas.mcp.server.call_tool` does, without a transport:
coerce, validate against the strict contract model, dispatch, attach the notes."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from atlas.contracts.mcp_models import TOOLS
from atlas.mcp.coerce import coerce
from atlas.mcp.server import _with_notes  # pyright: ignore[reportPrivateUsage]
from atlas.mcp.tools import AtlasTools

SPECS = {spec.name: spec for spec in TOOLS}


def call_tool(tools: AtlasTools, name: str, arguments: dict[str, Any]) -> BaseModel:
    """Raises pydantic.ValidationError exactly where the server would answer an error."""
    spec = SPECS[name]
    corrected, notes = coerce(name, arguments)
    request = spec.input_model.model_validate(corrected)
    result: BaseModel = getattr(tools, name.removeprefix("atlas_"))(request)
    return _with_notes(result, notes) if notes else result
