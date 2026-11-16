import sqlite3
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request

from testmart import auth
from testmart.db import get_setting, set_setting
from testmart.web import get_db, redirect, render

router = APIRouter(dependencies=[Depends(auth.require_user)])
Db = Annotated[sqlite3.Connection, Depends(get_db)]

# tab slug -> (setting key, checkbox label) pairs
TABS: dict[str, list[tuple[str, str]]] = {
    "general": [("maintenance_banner", "Maintenance banner"), ("compact_tables", "Compact tables")],
    "notifications": [("email_alerts", "Email alerts"), ("sms_alerts", "SMS alerts")],
}


@router.get("/settings")
def settings_index():
    return redirect("/settings/general")


@router.get("/settings/{tab}")
def settings_tab(request: Request, conn: Db, tab: str):
    if tab not in TABS:
        raise HTTPException(404, "Unknown settings tab")
    fields = [(key, label, get_setting(conn, key) == "1") for key, label in TABS[tab]]
    return render(request, conn, "settings.html", tab=tab, fields=fields)


@router.post("/settings/{tab}")
async def settings_save(request: Request, conn: Db, tab: str):
    if tab not in TABS:
        raise HTTPException(404, "Unknown settings tab")
    form = await request.form()
    with conn:
        for key, _ in TABS[tab]:
            set_setting(conn, key, "1" if form.get(key) == "on" else "0")
    return redirect(f"/settings/{tab}")
