"""Pick the exploration model client from settings. The only place provider types are chosen."""

from __future__ import annotations

from atlas.adapters.model.client import AnthropicModelClient
from atlas.adapters.model.ledger import BudgetedModelClient, ledger_from_settings
from atlas.adapters.model.openai_compat import DEFAULT_BASE_URL, OpenAICompatModelClient
from atlas.contracts.errors import ModelUnavailableError
from atlas.contracts.ports import ModelClient
from atlas.settings import Settings


def model_api_key(settings: Settings) -> str:
    """The configured key for the selected provider; raises when there is none (never logged)."""
    secret = settings.model_api_key
    if (secret is None or not secret.get_secret_value()) and settings.model_provider == "anthropic":
        secret = settings.anthropic_api_key
    if secret is None or not secret.get_secret_value():
        raise ModelUnavailableError(f"no API key configured for model provider {settings.model_provider!r}")
    return secret.get_secret_value()


def model_base_url(settings: Settings) -> str:
    return settings.model_base_url or DEFAULT_BASE_URL


def build_model_client(settings: Settings) -> ModelClient:
    """The provider client, always behind the spend ledger (R-COST-1)."""
    return BudgetedModelClient(_provider_client(settings), ledger_from_settings(settings))


def _provider_client(settings: Settings) -> ModelClient:
    key = model_api_key(settings)
    if settings.model_provider == "openai":
        return OpenAICompatModelClient(key, settings.atlas_model_id, model_base_url(settings))
    if settings.model_provider == "anthropic":
        return AnthropicModelClient(key, settings.atlas_model_id)
    raise ModelUnavailableError(f"unknown model provider {settings.model_provider!r}")
