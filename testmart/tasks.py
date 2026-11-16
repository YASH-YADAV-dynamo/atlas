"""Parameterised task templates with programmatic success checks against the TestMart DB."""
import random
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal

from testmart.db import connect
from testmart.pages_settings import TABS
from testmart.seed import make_address, populate

Params = dict[str, str]
ParamGen = Callable[[sqlite3.Connection, random.Random], Params]
Check = Callable[[sqlite3.Connection, Params, str], bool]


@dataclass(frozen=True)
class TaskTemplate:
    id: str
    text_template: str
    gen_params: ParamGen
    check: Check
    needs_answer: bool = False


@dataclass(frozen=True)
class Task:
    template_id: str
    text: str
    params: Params
    needs_answer: bool

    def check(self, db_path: str, answer: str = "") -> bool:
        """True if the DB (and, for read-only tasks, `answer`) shows the task was completed."""
        conn = connect(db_path)
        try:
            return TEMPLATES[self.template_id].check(conn, self.params, answer)
        finally:
            conn.close()


def _one(conn: sqlite3.Connection, sql: str, args: tuple = ()) -> sqlite3.Row | None:
    return conn.execute(sql, args).fetchone()


def _gen_refund(conn: sqlite3.Connection, rng: random.Random) -> Params:
    rows = conn.execute("SELECT number FROM orders WHERE status != 'Refunded'").fetchall()
    return {"order_id": rng.choice(rows)["number"]}


def _check_refund(conn: sqlite3.Connection, p: Params, _: str) -> bool:
    row = _one(conn, "SELECT status FROM orders WHERE number = ?", (p["order_id"],))
    return row is not None and row["status"] == "Refunded"


def _gen_address(conn: sqlite3.Connection, rng: random.Random) -> Params:
    name = rng.choice(conn.execute("SELECT name FROM customers").fetchall())["name"]
    return {"name": name, "address": make_address(rng)}


def _check_address(conn: sqlite3.Connection, p: Params, _: str) -> bool:
    row = _one(conn, "SELECT address FROM customers WHERE name = ?", (p["name"],))
    return row is not None and row["address"].strip() == p["address"].strip()


def _gen_price(conn: sqlite3.Connection, rng: random.Random) -> Params:
    sku = rng.choice(conn.execute("SELECT sku FROM products").fetchall())["sku"]
    return {"sku": sku, "price": f"{rng.randint(5, 500)}.{rng.randint(0, 99):02d}"}


def _check_price(conn: sqlite3.Connection, p: Params, _: str) -> bool:
    row = _one(conn, "SELECT price_cents FROM products WHERE sku = ?", (p["sku"],))
    return row is not None and row["price_cents"] == int(Decimal(p["price"]) * 100)


def _gen_email(conn: sqlite3.Connection, rng: random.Random) -> Params:
    return {"name": rng.choice(conn.execute("SELECT name FROM customers").fetchall())["name"]}


def _check_email(conn: sqlite3.Connection, p: Params, answer: str) -> bool:
    row = _one(conn, "SELECT email FROM customers WHERE name = ?", (p["name"],))
    return row is not None and row["email"].lower() in answer.lower()


def _gen_setting(conn: sqlite3.Connection, rng: random.Random) -> Params:
    off = [
        (key, label)
        for fields in TABS.values()
        for key, label in fields
        if _one(conn, "SELECT value FROM settings WHERE key = ?", (key,))["value"] != "1"
    ]
    key, label = rng.choice(off)
    return {"setting": key, "label": label}


def _check_setting(conn: sqlite3.Connection, p: Params, _: str) -> bool:
    row = _one(conn, "SELECT value FROM settings WHERE key = ?", (p["setting"],))
    return row is not None and row["value"] == "1"


TEMPLATES: dict[str, TaskTemplate] = {
    t.id: t
    for t in (
        TaskTemplate("refund_order", "Refund order {order_id}", _gen_refund, _check_refund),
        TaskTemplate("change_address",
                     "Change the shipping address of customer {name} to {address}",
                     _gen_address, _check_address),
        TaskTemplate("set_price", "Set product {sku} price to {price}", _gen_price, _check_price),
        TaskTemplate("find_email", "Find the email of customer {name}", _gen_email, _check_email,
                     needs_answer=True),
        TaskTemplate("enable_setting", "Open settings and enable {label}", _gen_setting, _check_setting),
    )
}


def generate_tasks(n: int, seed: int) -> list[Task]:
    """Generate `n` tasks whose parameters come from the DB that `populate(conn, seed)` builds."""
    rng = random.Random(seed)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        populate(conn, seed)
        tasks = []
        for _ in range(n):
            template = rng.choice(list(TEMPLATES.values()))
            params = template.gen_params(conn, rng)
            tasks.append(Task(template.id, template.text_template.format(**params), params,
                              template.needs_answer))
        return tasks
    finally:
        conn.close()
