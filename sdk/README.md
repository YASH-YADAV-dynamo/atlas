# atlas-sdk

A thin Python client for the Atlas Route API. It depends on `httpx` only and speaks plain dicts in the
Route API's JSON shape.

```python
from atlas_sdk import Atlas

atlas = Atlas("http://127.0.0.1:8000", api_key, site="shop.example")
summary = atlas.session(driver).run("Refund order 1042", {"order_id": "1042"}, fallback=my_agent_step)
print(summary)  # RunSummary(success=True, steps=5, map_steps=5, fallback_calls=0)
```

- `driver` has `snapshot() -> dict` (url, title, elements as role/name dicts) and `act(action: dict)`.
- `fallback(snapshot, goal, params) -> dict | None` is your own agent step. It is called only where the
  map ends and returns one action in the Route API's JSON shape, or `None` when the goal is done.
- Parameter values are replaced by `{slot}` placeholders before a step is reported, so the map learns the
  road and not the order number. Known roads are driven with zero fallback calls.
- API errors raise `AtlasApiError(status, code, message)`.

`example.py` runs a TestMart refund through a Playwright page.
