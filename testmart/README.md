# TestMart

Self-hosted synthetic back-office app (FastAPI + Jinja2 + SQLite, no JS) used to benchmark browser agents.
All data is synthetic (`example.test` emails, "Testville" addresses).

## Run

```
pip install -r testmart/requirements.txt
python -m testmart            # http://0.0.0.0:8100
```

Environment (all optional; defaults are dev-only placeholders, never commit real values):

```
TESTMART_PASSWORD=testmart     # password for user "admin"
TESTMART_SECRET=change-me      # cookie signing key
TESTMART_DB=<tmpdir>/testmart/testmart.db
TESTMART_SEED=42               # seed used when the DB is first created
TESTMART_HOST=0.0.0.0
TESTMART_PORT=8100
```

Ops endpoints: `GET /healthz`, `POST /__reset[?seed=N]` (re-seeds, unauthenticated by design for the harness).
`ui_version` (default 1) is stored in `settings` and exposed to templates as `data-ui-version` on `<body>`; switch it with `POST|GET /admin/mutate?v=1..3` (unauthenticated like `/__reset`; `/__reset` restores v1):
v1 original; v2 order page link "Refund order" becomes "Issue refund" and the "Customers" nav link moves to a "More" page (`/more`; dashboard "Go to customers" stays);
v3 "Refund order" opens a review page (`/orders/{}/refund/review`) whose "Continue to refund" link opens the confirm page. `/more` is 404 outside v2, the review page outside v3.

Sanity check (needs `playwright install chromium`): `python -m testmart.selfcheck`

## Pages / states (13)

| # | State | URL template | Reached from |
|---|-------|--------------|--------------|
| 1 | Login | `/login` | any page when logged out |
| 2 | Dashboard | `/` | nav "Dashboard", login |
| 3 | Orders list (search, pagination) | `/orders` | nav "Orders", dashboard "Go to orders" |
| 4 | Order detail (refundable) | `/orders/{}` | link "View order" |
| 5 | Refund confirmation | `/orders/{}/refund` | link "Refund order" |
| 6 | Order detail (refunded; no "Refund order" link) | `/orders/{}` | button "Confirm refund" |
| 7 | Customers list (search, pagination) | `/customers` | nav "Customers", dashboard "Go to customers" |
| 8 | Customer detail | `/customers/{}` | link "View customer" (also from order detail) |
| 9 | Edit address | `/customers/{}/address` | link "Edit address" |
| 10 | Products list (pagination) | `/products` | nav "Products", dashboard "Go to products" |
| 11 | Product edit | `/products/{}/edit` | link "Edit product" |
| 12 | Settings: General | `/settings/general` | nav "Settings", dashboard "Open settings", tab "General" |
| 13 | Settings: Notifications | `/settings/notifications` | tab "Notifications" |

Conventions: every control is a real button/link/labelled input; list rows use one constant link name per list
("View order", "View customer", "Edit product"). Dynamic lists: orders, customers, products (10 per page).
Pagination links "Previous page"/"Next page" appear only when applicable, so list states have a few element-set variants.
Settings checkboxes (General: "Maintenance banner", "Compact tables"; Notifications: "Email alerts", "SMS alerts") save via "Save settings".

## Task templates (`testmart/tasks.py`)

`generate_tasks(n, seed) -> list[Task]`; `Task.check(db_path, answer="") -> bool`. Params come from the DB seeded with `seed`
(start the server with the same `TESTMART_SEED`/`/__reset?seed=`).

| id | text | check |
|----|------|-------|
| `refund_order` | Refund order {order_id} | order status is Refunded |
| `change_address` | Change the shipping address of customer {name} to {address} | customer address equals target |
| `set_price` | Set product {sku} price to {price} | product price equals target |
| `find_email` | Find the email of customer {name} | answer string contains the email (read-only) |
| `enable_setting` | Open settings and enable {label} | setting value is on |
