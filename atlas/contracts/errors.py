"""Typed errors and the single table that maps them to HTTP status codes."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType


class AtlasError(Exception):
    """Base for every error Atlas raises on purpose."""

    code: str = "internal"


class UnauthorizedError(AtlasError):
    code = "unauthorized"


class UnknownStateError(AtlasError):
    """A state id the map has never seen was used as a starting point."""

    code = "unknown_state"


class UnknownPlanError(AtlasError):
    """`/v1/finish` named an expired or never issued plan and gave nothing else to go on."""

    code = "unknown_plan"


class MissingParamError(AtlasError):
    """An action needs a `{slot}` the caller did not supply."""

    code = "missing_param"


class UnsafeQueryValueError(AtlasError):
    """A value that must be inlined into Cypher failed validation."""

    code = "unsafe_query_value"


class PathFormatError(AtlasError):
    """The engine returned a path the adapter cannot interpret."""

    code = "path_format"


class StoreUnavailableError(AtlasError):
    """HydraDB cannot be reached or timed out."""

    code = "store_unavailable"


class ModelUnavailableError(AtlasError):
    code = "model_unavailable"


class ToolUnavailableError(AtlasError):
    """An MCP tool was called in a mode that does not offer it (atlas_run without a browser)."""

    code = "tool_unavailable"


class TraceFormatError(AtlasError):
    """A trace file line is not valid JSON or does not fit the TraceStep schema."""

    code = "trace_format"

    def __init__(self, message: str, line: int = 0) -> None:
        super().__init__(message)
        self.line = line


class BudgetExceededError(AtlasError):
    """The model spend cap is reached; raised before the provider is called."""

    code = "budget_exceeded"


class BrowserActionError(AtlasError):
    """The browser could not execute an action (locator not found, timeout)."""

    code = "browser_action_failed"


HTTP_STATUS: Mapping[type[AtlasError], int] = MappingProxyType(
    {
        UnauthorizedError: 401,
        UnknownStateError: 404,
        UnknownPlanError: 404,
        MissingParamError: 422,
        UnsafeQueryValueError: 400,
        PathFormatError: 502,
        StoreUnavailableError: 503,
        ModelUnavailableError: 503,
        BrowserActionError: 502,
        BudgetExceededError: 429,
        TraceFormatError: 422,
        ToolUnavailableError: 409,
    }
)


def http_status_for(error: AtlasError) -> int:
    """HTTP status for an error; subclasses inherit their parent's mapping."""
    for kind in type(error).__mro__:
        if kind in HTTP_STATUS:
            return HTTP_STATUS[kind]
    return 500
