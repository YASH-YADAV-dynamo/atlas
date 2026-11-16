import math
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from testmart.config import load
from testmart.db import connect, format_money, get_setting

PAGE_SIZE = 10
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
templates.env.filters["money"] = format_money


def get_db() -> Iterator[sqlite3.Connection]:
    conn = connect(load().db_path)
    try:
        yield conn
    finally:
        conn.close()


def render(request: Request, conn: sqlite3.Connection, name: str, **ctx: Any):
    """Render a template with the shared context (ui_version is read here for later mutations)."""
    ctx["ui_version"] = int(get_setting(conn, "ui_version", "1"))
    return templates.TemplateResponse(request, name, ctx)


def redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def page_bounds(total: int, page: int) -> tuple[int, int, int]:
    """Return (clamped page, offset, page count) for a list of `total` rows."""
    pages = max(1, math.ceil(total / PAGE_SIZE))
    page = min(page, pages)
    return page, (page - 1) * PAGE_SIZE, pages
