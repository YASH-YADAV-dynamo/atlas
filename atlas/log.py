"""JSON-line logging. Callers pass ids, counters and codes only: never parameter values,
secrets or page text."""

from __future__ import annotations

import json
import logging
import sys
from typing import Final

_LOGGER: Final = logging.getLogger("atlas")
_ALLOWED = (str, int, float, bool, type(None))


def _configure() -> None:
    if _LOGGER.handlers:
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(message)s"))
    _LOGGER.addHandler(handler)
    _LOGGER.setLevel(logging.INFO)
    _LOGGER.propagate = False


def log_event(event: str, task_id: str | None = None, **fields: object) -> None:
    """Emit one JSON line. Non-scalar field values are dropped, so structures with data cannot leak."""
    _configure()
    record: dict[str, object] = {"event": event}
    if task_id is not None:
        record["task_id"] = task_id
    record.update({key: value for key, value in fields.items() if isinstance(value, _ALLOWED)})
    _LOGGER.info(json.dumps(record, sort_keys=True))
