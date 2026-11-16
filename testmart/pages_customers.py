import sqlite3
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request

from testmart import auth
from testmart.web import PAGE_SIZE, get_db, page_bounds, redirect, render

router = APIRouter(dependencies=[Depends(auth.require_user)])
Db = Annotated[sqlite3.Connection, Depends(get_db)]


def _get_customer(conn: sqlite3.Connection, customer_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM customers WHERE id = ?", (customer_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "Customer not found")
    return row


@router.get("/customers")
def customers_list(request: Request, conn: Db, q: Annotated[str, Query(max_length=64)] = "",
                   page: Annotated[int, Query(ge=1)] = 1):
    like = f"%{q}%"
    where = "WHERE name LIKE ? OR email LIKE ?"
    total = conn.execute(f"SELECT COUNT(*) FROM customers {where}", (like, like)).fetchone()[0]
    page, offset, pages = page_bounds(total, page)
    rows = conn.execute(
        f"SELECT * FROM customers {where} ORDER BY id LIMIT ? OFFSET ?", (like, like, PAGE_SIZE, offset)
    ).fetchall()
    return render(request, conn, "customers.html", rows=rows, q=q, page=page, pages=pages)


@router.get("/customers/{customer_id}")
def customer_detail(request: Request, conn: Db, customer_id: int):
    return render(request, conn, "customer_detail.html", customer=_get_customer(conn, customer_id))


@router.get("/customers/{customer_id}/address")
def address_form(request: Request, conn: Db, customer_id: int):
    return render(request, conn, "address_form.html", customer=_get_customer(conn, customer_id), error="")


@router.post("/customers/{customer_id}/address")
def address_submit(request: Request, conn: Db, customer_id: int,
                   address: Annotated[str, Form(max_length=200)]):
    customer = _get_customer(conn, customer_id)
    address = address.strip()
    if len(address) < 5:
        return render(request, conn, "address_form.html", customer=customer,
                      error="Address must be at least 5 characters.")
    with conn:
        conn.execute("UPDATE customers SET address = ? WHERE id = ?", (address, customer_id))
    return redirect(f"/customers/{customer_id}")
