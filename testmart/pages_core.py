import sqlite3
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from testmart import auth
from testmart.config import load
from testmart.db import connect, get_setting, set_setting
from testmart.seed import populate
from testmart.web import get_db, redirect, render

router = APIRouter()
Db = Annotated[sqlite3.Connection, Depends(get_db)]


@router.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.post("/__reset")
def reset(seed: int | None = None) -> JSONResponse:
    cfg = load()
    use_seed = cfg.seed if seed is None else seed
    conn = connect(cfg.db_path)
    try:
        populate(conn, use_seed)
    finally:
        conn.close()
    return JSONResponse({"status": "reset", "seed": use_seed})


@router.api_route("/admin/mutate", methods=["GET", "POST"])
def mutate(v: Annotated[int, Query(ge=1, le=3)]) -> JSONResponse:
    """Switch the UI version (1 original, 2 renamed/moved controls, 3 refund interstitial). Unauthenticated."""
    conn = connect(load().db_path)
    try:
        with conn:
            set_setting(conn, "ui_version", str(v))
    finally:
        conn.close()
    return JSONResponse({"ui_version": v})


@router.get("/more", dependencies=[Depends(auth.require_user)])
def more(request: Request, conn: Db):
    if int(get_setting(conn, "ui_version", "1")) != 2:
        raise HTTPException(404, "Not found")
    return render(request, conn, "more.html")


@router.get("/login")
def login_page(request: Request, conn: Db):
    return render(request, conn, "login.html", error=False)


@router.post("/login")
def login_submit(request: Request, conn: Db, username: Annotated[str, Form(max_length=64)],
                 password: Annotated[str, Form(max_length=128)]):
    if not auth.check_credentials(username, password):
        return render(request, conn, "login.html", error=True)
    response = redirect("/")
    response.set_cookie(auth.COOKIE, auth.make_token(), httponly=True, samesite="lax")
    return response


@router.post("/logout")
def logout():
    response = redirect("/login")
    response.delete_cookie(auth.COOKIE)
    return response


@router.get("/", dependencies=[Depends(auth.require_user)])
def dashboard(request: Request, conn: Db):
    counts = {
        table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # fixed table names only
        for table in ("orders", "customers", "products")
    }
    return render(request, conn, "dashboard.html", counts=counts)
