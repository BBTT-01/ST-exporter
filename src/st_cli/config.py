"""Settings loaded from .env via pydantic-settings."""

from __future__ import annotations

from enum import Enum
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(str, Enum):
    PRODUCTION = "production"
    INTEGRATION = "integration"

    @property
    def auth_url(self) -> str:
        if self is Environment.INTEGRATION:
            return "https://auth-integration.servicetitan.io/connect/token"
        return "https://auth.servicetitan.io/connect/token"

    @property
    def api_base(self) -> str:
        if self is Environment.INTEGRATION:
            return "https://api-integration.servicetitan.io"
        return "https://api.servicetitan.io"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ST_",
        env_file=".env",
        env_file_encoding="utf-8",
    )

    client_id: str
    client_secret: str = Field(repr=False)
    app_key: str = Field(repr=False)
    tenant_id: int
    environment: Environment = Environment.PRODUCTION

    @property
    def auth_url(self) -> str:
        return self.environment.auth_url

    @property
    def api_base(self) -> str:
        return self.environment.api_base


def load_settings(env_file: Path | None = None) -> Settings:
    """Load settings, optionally from a specific .env file."""
    kwargs: dict = {}
    if env_file is not None:
        kwargs["_env_file"] = str(env_file)
    return Settings(**kwargs)  # type: ignore[arg-type]
