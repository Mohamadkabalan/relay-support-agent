"""Runtime settings, read from environment variables.

Every setting is documented in `.env.example` at the repository root. Docker Compose builds
`DATABASE_URL` and `REDIS_URL` for the containers; tests and local tools pass their own.
"""

from functools import lru_cache
from typing import Literal

from pydantic import AliasChoices, Field, PostgresDsn, RedisDsn
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_MCP_ALLOWED_HOSTS = (
    "mcp",
    "mcp:*",
    "localhost",
    "localhost:*",
    "127.0.0.1",
    "127.0.0.1:*",
)


class Settings(BaseSettings):
    """Configuration for every Relay process (API, MCP server, CLI)."""

    model_config = SettingsConfigDict(
        env_prefix="RELAY_",
        extra="ignore",
        frozen=True,
        validate_by_name=True,  # lets tests pass Settings(database_url=...) by field name
    )

    env: Literal["dev", "test", "prod"] = "dev"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    store_name: str = "Pixel & Volt"

    database_url: PostgresDsn = Field(
        validation_alias=AliasChoices("DATABASE_URL", "RELAY_DATABASE_URL"),
    )
    redis_url: RedisDsn = Field(
        validation_alias=AliasChoices("REDIS_URL", "RELAY_REDIS_URL"),
    )

    # Seconds each dependency gets to answer a readiness probe.
    readiness_timeout_s: float = Field(default=2.0, gt=0)

    # Uvicorn trusts X-Forwarded-* headers only from these addresses. Compose sets "*" because
    # the API is reachable only through Caddy on the internal network.
    forwarded_allow_ips: str = "127.0.0.1"

    # Host headers the MCP server accepts (DNS-rebinding protection). "name:*" matches any port.
    mcp_allowed_hosts: tuple[str, ...] = DEFAULT_MCP_ALLOWED_HOSTS


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings, read once from the environment."""
    return Settings()
