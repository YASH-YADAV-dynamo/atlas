"""Keep keys embedded in `/mcp/k/<key>` out of logs.

uvicorn's access log prints the request path; `install_access_log_scrub()` puts a filter on that
logger (and on `uvicorn.error`, `httpx`, `httpcore`) that masks the key segment before any handler
formats the record.
"""

from __future__ import annotations

import logging
import re

_KEY_IN_PATH = re.compile(r"(/(?:mcp)/k/)[^\s/?#\"'%]+")
MASK = r"\1***"
_LOGGERS = ("uvicorn.access", "uvicorn.error", "uvicorn", "httpx", "httpcore", "mcp")


def scrub(text: str) -> str:
    return _KEY_IN_PATH.sub(MASK, text)


class KeyScrubFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        """Mask the key in the arguments (uvicorn's access formatter needs them as a tuple) and,
        when the key was formatted into the message text itself, in the message."""
        if isinstance(record.args, tuple):
            record.args = tuple(scrub(a) if isinstance(a, str) else a for a in record.args)
        elif isinstance(record.args, dict):
            record.args = {k: scrub(v) if isinstance(v, str) else v for k, v in record.args.items()}
        if isinstance(record.msg, str):
            record.msg = scrub(record.msg)
        return True


def install_access_log_scrub() -> None:
    """Idempotent. Filters on a logger run for records logged on it, so install on each name."""
    for name in _LOGGERS:
        logger = logging.getLogger(name)
        if not any(isinstance(f, KeyScrubFilter) for f in logger.filters):
            logger.addFilter(KeyScrubFilter())
