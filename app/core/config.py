"""Application configuration.

All runtime configuration is environment driven (12-factor). Defaults are safe
for local development; anything touching real infrastructure must be supplied
through the environment.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["local", "staging", "production"]
ProviderMode = Literal["auto", "aws", "mock"]

# The value shipped in .env.example. Fine locally, dangerous anywhere public:
# it is in the repository, so it authenticates everybody.
DEFAULT_API_KEY = "local-dev-key"


class Settings(BaseSettings):
    """Typed, validated application settings.

    Every field can be overridden with a ``DASH_``-prefixed environment
    variable, e.g. ``DASH_AWS_REGION=eu-west-1``.
    """

    model_config = SettingsConfigDict(
        env_prefix="DASH_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @model_validator(mode="after")
    def _refuse_default_key_in_production(self) -> Settings:
        """Fail at startup rather than serving a public API with a known key.

        ``DASH_ENVIRONMENT=production`` plus the default key would mean anyone
        who has read this repository can read the account's inventory and cost
        data. Deploying with a placeholder secret is one of the most common
        production mistakes, and it is silent — the app looks healthy. So it is
        a hard failure, with the two legitimate ways out spelled out.
        """
        if (
            self.environment == "production"
            and self.auth_enabled
            and self.api_key == DEFAULT_API_KEY
        ):
            raise ValueError(
                "DASH_API_KEY is still the development default while "
                "DASH_ENVIRONMENT=production. Anyone who can read this "
                "repository could read your account's inventory and cost data. "
                'Generate a real key: python -c "import secrets; '
                'print(secrets.token_urlsafe(32))" — or set '
                "DASH_AUTH_ENABLED=false for a deliberately public read-only demo."
            )
        return self

    # -- Application ------------------------------------------------------
    app_name: str = "Cloud Control API"
    version: str = "1.0.0"
    environment: Environment = "local"

    # -- AWS --------------------------------------------------------------
    aws_region: str = "us-east-1"
    # Cost Explorer is a global service whose API only exists in us-east-1.
    # Kept separate from aws_region because that asymmetry is a real gotcha.
    cost_explorer_region: str = "us-east-1"
    aws_profile: str | None = None
    provider_mode: ProviderMode = "auto"
    aws_timeout_seconds: int = 10

    # -- Security ---------------------------------------------------------
    api_key: str = DEFAULT_API_KEY
    auth_enabled: bool = True
    # Local-only convenience: inject the dev API key into the dashboard HTML so
    # `make run` works with zero setup. Refused outside local/staging.
    bootstrap_dashboard: bool = True

    # -- Behaviour --------------------------------------------------------
    cache_ttl_seconds: int = 60
    cors_origins: str = "http://localhost:8000,http://127.0.0.1:8000"
    cost_currency: str = "USD"
    monthly_budget: float = 8000.0
    log_level: str = "INFO"

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def api_key_injected(self) -> bool:
        """Whether the dashboard may receive the API key at render time.

        Local development only. Any shared deployment must supply the key to the
        browser by its own means (a login flow, a proxy that injects the header)
        rather than having the server print it into the page source.
        """
        return self.bootstrap_dashboard and self.environment == "local"

    @property
    def effective_api_key(self) -> str | None:
        return self.api_key if self.auth_enabled else None

    def public_fingerprint(self) -> dict[str, object]:
        """Non-sensitive settings that are safe to expose over the API."""
        return {
            "app_name": self.app_name,
            "version": self.version,
            "environment": self.environment,
            "region": self.aws_region,
            "cost_explorer_region": self.cost_explorer_region,
            "provider_mode": self.provider_mode,
            "currency": self.cost_currency,
            "monthly_budget": self.monthly_budget,
            "cache_ttl_seconds": self.cache_ttl_seconds,
            "auth_enabled": self.auth_enabled,
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings accessor (import-safe, testable via ``cache_clear``)."""
    return Settings()
