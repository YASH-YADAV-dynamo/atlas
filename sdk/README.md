# atlas-route-sdk

A thin Python client for the Atlas Route API (https://atlas.yashdynamo.fun). It depends on `httpx` only and
speaks plain dicts in the Route API's JSON shape. The import name is `atlas_sdk`.

```
pip install atlas-route-sdk                 # core client
pip install "atlas-route-sdk[playwright]"   # plus the Playwright driver; then: playwright install chromium
```

A run costs one `POST /v1/start` and one `POST /v1/finish`. Known roads and remembered answer pages are
followed with no model call; your own agent (`fallback`) is asked only where the map ends.

```python
from playwright.sync_api import sync_playwright
from atlas_sdk import Atlas
from atlas_sdk.drivers.playwright import PlaywrightDriver

with sync_playwright() as pw:
    page = pw.chromium.launch().new_page()
    page.goto("https://en.wikipedia.org/wiki/Main_Page")
    atlas = Atlas("https://atlas.yashdynamo.fun", api_key, site="en.wikipedia.org")
    summary = atlas.session(PlaywrightDriver(page)).run(
        "Open the Wikipedia article about {topic}", {"topic": "Kajol"}, fallback=my_agent_step, kind="read"
    )
    print(summary)  # RunSummary(success=True, steps=1, map_steps=1, fallback_calls=0)
    print(
        summary.via, summary.reason
    )  # "route" | "url" | "explore", and why the first plan was served or not
```

Get a key with `POST https://atlas.yashdynamo.fun/v1/keys`.

- `driver` has `snapshot() -> dict` (url, title, elements as role/name dicts) and `act(action: dict)`.
  `PlaywrightDriver` waits for network idle and for the element count to settle before it snapshots.
- `fallback(snapshot, goal, params) -> dict | None` is your own agent step: one action in the Route API's
  JSON shape, or `None` when the goal is done.
- Parameter values become `{slot}` placeholders before anything is reported, so the map learns the road and
  not the order number. Standalone form fills are folded into the action that follows them.
- `run(..., kind="read")` marks a lookup: the answer page's address is remembered and the next run opens it
  directly (`via="url"`).
- API errors raise `AtlasApiError(status, code, message)`.
- `from atlas_sdk.playwright import PlaywrightDriver` still works as a re-export.

`example.py` runs a TestMart refund through a Playwright page.
