import random
import sqlite3

from testmart.db import SCHEMA, set_setting

FIRST = ["Alex", "Bina", "Chen", "Dara", "Eli", "Fay", "Gus", "Hana", "Ivo", "Jun",
         "Kai", "Lena", "Milo", "Nia", "Omar", "Pia", "Quin", "Rhea", "Sol", "Tove"]
LAST = ["Testwell", "Samplewood", "Mockridge", "Fakeman", "Dummett", "Placeholder",
        "Syntheta", "Stubbs", "Fixture", "Demoson"]
STREETS = ["Sample", "Mock", "Test", "Demo", "Fixture", "Dummy"]
PRODUCT_WORDS = (["Blue", "Red", "Mini", "Mega", "Eco", "Pro"],
                 ["Widget", "Gadget", "Gizmo", "Sprocket", "Lamp", "Cable"])
SETTINGS = {
    "maintenance_banner": "0", "compact_tables": "0",
    "email_alerts": "0", "sms_alerts": "0",
}
N_CUSTOMERS, N_PRODUCTS, N_ORDERS = 30, 20, 60


def make_address(rng: random.Random) -> str:
    return f"{rng.randint(1, 999)} {rng.choice(STREETS)} Street, Testville {rng.randint(100000, 999999)}"


def populate(conn: sqlite3.Connection, seed: int) -> None:
    """Drop and recreate all tables, filled deterministically from `seed`."""
    rng = random.Random(seed)
    conn.executescript(SCHEMA)
    names = rng.sample([f"{f} {last}" for f in FIRST for last in LAST], N_CUSTOMERS)
    for i, name in enumerate(names, start=1):
        email = f"{name.lower().replace(' ', '.')}@example.test"
        conn.execute("INSERT INTO customers VALUES (?, ?, ?, ?)", (i, name, email, make_address(rng)))
    names = rng.sample([f"{a} {b}" for a in PRODUCT_WORDS[0] for b in PRODUCT_WORDS[1]], N_PRODUCTS)
    prices = []
    for i, name in enumerate(names, start=1):
        prices.append(rng.randint(500, 20000))
        conn.execute("INSERT INTO products VALUES (?, ?, ?, ?)", (i, f"TM-{1000 + i}", name, prices[-1]))
    for i in range(1, N_ORDERS + 1):
        product_id, qty = rng.randint(1, N_PRODUCTS), rng.randint(1, 5)
        status = rng.choices(["Paid", "Shipped", "Refunded"], weights=[5, 4, 1])[0]
        conn.execute(
            "INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?, ?)",
            (i, f"ORD-{1000 + i}", rng.randint(1, N_CUSTOMERS), product_id, qty,
             prices[product_id - 1] * qty, status),
        )
    for key, value in SETTINGS.items():
        set_setting(conn, key, value)
    set_setting(conn, "ui_version", "1")
    set_setting(conn, "seed", str(seed))
    conn.commit()
