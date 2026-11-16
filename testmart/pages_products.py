import sqlite3
from decimal import Decimal, InvalidOperation
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request

from testmart import auth
from testmart.web import PAGE_SIZE, get_db, page_bounds, redirect, render

router = APIRouter(dependencies=[Depends(auth.require_user)])
Db = Annotated[sqlite3.Connection, Depends(get_db)]
MAX_PRICE_CENTS = 10_000_000


def parse_price(text: str) -> int | None:
    """Convert a decimal price string to cents; None if invalid or out of range."""
    try:
        value = Decimal(text.strip())
    except InvalidOperation:
        return None
    cents = value * 100
    if not value.is_finite() or cents != cents.to_integral_value() or not 0 < cents <= MAX_PRICE_CENTS:
        return None
    return int(cents)


def _get_product(conn: sqlite3.Connection, product_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "Product not found")
    return row


@router.get("/products")
def products_list(request: Request, conn: Db, page: Annotated[int, Query(ge=1)] = 1):
    total = conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]
    page, offset, pages = page_bounds(total, page)
    rows = conn.execute(
        "SELECT * FROM products ORDER BY id LIMIT ? OFFSET ?", (PAGE_SIZE, offset)
    ).fetchall()
    return render(request, conn, "products.html", rows=rows, page=page, pages=pages)


@router.get("/products/{product_id}/edit")
def product_edit(request: Request, conn: Db, product_id: int):
    return render(request, conn, "product_edit.html", product=_get_product(conn, product_id), error="")


@router.post("/products/{product_id}/edit")
def product_save(request: Request, conn: Db, product_id: int, price: Annotated[str, Form(max_length=20)]):
    product = _get_product(conn, product_id)
    cents = parse_price(price)
    if cents is None:
        return render(request, conn, "product_edit.html", product=product,
                      error="Enter a valid price, for example 19.99.")
    with conn:
        conn.execute("UPDATE products SET price_cents = ? WHERE id = ?", (cents, product_id))
    return redirect("/products")
