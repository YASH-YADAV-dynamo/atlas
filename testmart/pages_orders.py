import sqlite3
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from testmart import auth
from testmart.db import get_setting
from testmart.web import PAGE_SIZE, get_db, page_bounds, redirect, render

router = APIRouter(dependencies=[Depends(auth.require_user)])
Db = Annotated[sqlite3.Connection, Depends(get_db)]

ORDER_SELECT = (
    "SELECT o.*, c.name AS customer_name, c.id AS customer_id, p.name AS product_name "
    "FROM orders o JOIN customers c ON c.id = o.customer_id JOIN products p ON p.id = o.product_id"
)


def _get_order(conn: sqlite3.Connection, order_id: int) -> sqlite3.Row:
    row = conn.execute(f"{ORDER_SELECT} WHERE o.id = ?", (order_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "Order not found")
    return row


@router.get("/orders")
def orders_list(request: Request, conn: Db, q: Annotated[str, Query(max_length=64)] = "",
                page: Annotated[int, Query(ge=1)] = 1):
    like = f"%{q}%"
    where = "WHERE o.number LIKE ? OR c.name LIKE ?"
    total = conn.execute(
        "SELECT COUNT(*) FROM orders o JOIN customers c ON c.id = o.customer_id " + where,
        (like, like),
    ).fetchone()[0]
    page, offset, pages = page_bounds(total, page)
    rows = conn.execute(
        f"{ORDER_SELECT} {where} ORDER BY o.id LIMIT ? OFFSET ?", (like, like, PAGE_SIZE, offset)
    ).fetchall()
    return render(request, conn, "orders.html", rows=rows, q=q, page=page, pages=pages)


@router.get("/orders/{order_id}")
def order_detail(request: Request, conn: Db, order_id: int):
    return render(request, conn, "order_detail.html", order=_get_order(conn, order_id))


@router.get("/orders/{order_id}/refund/review")
def refund_review(request: Request, conn: Db, order_id: int):
    if int(get_setting(conn, "ui_version", "1")) != 3:
        raise HTTPException(404, "Not found")
    order = _get_order(conn, order_id)
    if order["status"] == "Refunded":
        return redirect(f"/orders/{order_id}")
    return render(request, conn, "refund_review.html", order=order)


@router.get("/orders/{order_id}/refund")
def refund_confirm(request: Request, conn: Db, order_id: int):
    order = _get_order(conn, order_id)
    if order["status"] == "Refunded":
        return redirect(f"/orders/{order_id}")
    return render(request, conn, "refund_confirm.html", order=order)


@router.post("/orders/{order_id}/refund")
def refund_submit(conn: Db, order_id: int):
    _get_order(conn, order_id)
    with conn:
        conn.execute("UPDATE orders SET status = 'Refunded' WHERE id = ?", (order_id,))
    return redirect(f"/orders/{order_id}")
