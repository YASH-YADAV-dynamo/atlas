"""All configuration, read from the environment (see .env.example)."""

from __future__ import annotations

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", populate_by_name=True)

    hydradb_url: str = "bolt://127.0.0.1:7687"
    hydradb_token: SecretStr = SecretStr("")
    hydradb_graph: str = "default"
    atlas_store: str = "hydradb"  # or "memory" (tests and offline demos only; never a silent fallback)

    atlas_api_key: SecretStr | None = None  # when set, /v1/* requires `Authorization: Bearer`
    atlas_namespace: str = "demo"  # Release 0: one tenant per deployment
    atlas_api_url: str = "http://127.0.0.1:8000"

    # Hosted MCP and tenants (see docs/mcp.md). The admin key above owns `atlas_namespace`;
    # the demo key reads it; self-serve keys (POST /v1/keys) each own a private namespace.
    atlas_demo_mcp_key: SecretStr | None = None  # public, read-only, bound to `atlas_namespace`
    atlas_key_pepper: SecretStr | None = None  # HMAC pepper for stored key hashes; unset = no self-serve keys
    atlas_keys_path: str = "data/mcp_keys.json"  # durable tenant key store (hashes only)
    atlas_public_url: str | None = None  # e.g. https://atlas.example.com; else derived from the request
    tenant_max: int = 200  # self-serve tenants in total
    tenant_keys_per_ip_day: int = 3
    tenant_max_events: int = 5000  # reported steps stored per tenant
    tenant_max_plan_states: int = 2000  # new pages a private key may add to its map via plan
    tenant_requests_per_minute: int = 120  # per key (per key and address for the demo key)

    # Exploration model, provider-agnostic: "anthropic" (SDK) or "openai" (any OpenAI-compatible
    # chat-completions endpoint at `model_base_url`). `model_api_key` falls back to the Anthropic key.
    model_provider: str = "anthropic"
    model_base_url: str | None = None
    model_api_key: SecretStr | None = Field(
        default=None, validation_alias=AliasChoices("MODEL_API_KEY", "DEEPSEEK_API_KEY")
    )
    anthropic_api_key: SecretStr | None = None
    atlas_model_id: str = "claude-sonnet-5-5"

    testmart_url: str = "http://127.0.0.1:8100"
    testmart_user: str = "admin"
    testmart_password: SecretStr = SecretStr("testmart")  # TestMart's dev-only default
    plan_max_steps: int = 8
    plan_max_steps_cap: int = 8  # engine limit per path query (ADR-0005)
    plan_path_count: int = 5
    run_max_steps: int = 25

    # MCP server: "runner" owns a browser on `mcp_site`; "planner" has no browser.
    mcp_mode: str = "runner"
    mcp_site: str = "testmart"

    # Cost guard: every model call is charged to a ledger file with a hard cap (USD).
    model_price_in_per_m: float = 0.30  # USD per million input tokens (cache-miss price)
    model_price_out_per_m: float = 1.20
    model_ledger_path: str = "data/model_ledger.json"
    model_budget_usd: float = 1.50
    demo_model_runs_per_ip_hour: int = 3
    demo_max_explore_steps: int = 8

    # Defaults suit a small host: at most 2 browser runs at once, the rest queue.
    demo_max_concurrent_runs: int = 2
    demo_max_queued_runs: int = 8
