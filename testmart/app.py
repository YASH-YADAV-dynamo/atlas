from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse

from testmart import pages_core, pages_customers, pages_orders, pages_products, pages_settings
from testmart.auth import LoginRequired
from testmart.config import load
from testmart.db import connect, is_initialised
from testmart.seed import populate


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    cfg = load()
    conn = connect(cfg.db_path)
    try:
        if not is_initialised(conn):
            populate(conn, cfg.seed)
    finally:
        conn.close()
    yield


app = FastAPI(title="TestMart", lifespan=lifespan)


@app.exception_handler(LoginRequired)
def _login_required(_: Request, __: LoginRequired) -> RedirectResponse:
    return RedirectResponse("/login", status_code=303)


for module in (pages_core, pages_orders, pages_customers, pages_products, pages_settings):
    app.include_router(module.router)
