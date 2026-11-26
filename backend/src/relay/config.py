"""Runtime settings, read from environment variables.

Every setting is documented in `.env.example` at the repository root. Docker Compose builds
`DATABASE_URL` and `REDIS_URL` for the containers; tests and local tools pass their own.
"""

from functools import lru_cache
from typing import Literal, Self

from pydantic import (
    AliasChoices,
    Field,
    PostgresDsn,
    RedisDsn,
    SecretStr,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

# .env.example ships development secrets containing this marker; production refuses them.
DEV_SECRET_MARKER = "dev-only"  # noqa: S105 (a marker to refuse, not a secret)

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
        hide_input_in_errors=True,  # a rejected value may be a secret: keep it out of logs
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

    # HS256 key the API signs per-turn tokens with and the MCP server verifies them with
    # (PLAN.md §6.1). Required to serve MCP over HTTP; not used over stdio.
    mcp_jwt_secret: SecretStr | None = Field(default=None, min_length=32)

    # The customer a stdio MCP server (Claude Desktop) acts for: an email address or customer ID.
    mcp_customer: str | None = None

    # Largest MCP request body the HTTP server accepts.
    mcp_max_request_bytes: int = Field(default=64 * 1024, gt=0)

    # ---------------------------------------------------------------- agent runtime (Phase 3)

    # Where the agent reaches the MCP server (inside the compose network).
    mcp_url: str = "http://mcp:8001/mcp"

    # The model provider behind the agent, and its credentials. Keys are read only from the
    # environment; the providers' own variable names are used so the SDKs' docs apply.
    provider: Literal["anthropic", "openai"] = "anthropic"
    anthropic_api_key: SecretStr | None = Field(
        default=None, validation_alias=AliasChoices("ANTHROPIC_API_KEY", "anthropic_api_key")
    )
    openai_api_key: SecretStr | None = Field(
        default=None, validation_alias=AliasChoices("OPENAI_API_KEY", "openai_api_key")
    )
    agent_model: str = "claude-opus-5-5"  # D2
    agent_effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"
    openai_model: str = "gpt-5.5"  # in openai 3.60.0's model list; not run live in this repo
    # Server-side refusal fallbacks (`fallbacks: "default"`). Off for model-comparison runs.
    agent_fallbacks: bool = True
    # What the API does when replayed history no longer matches a thinking block: "error" fails
    # the request (development, CI and evals, so a history edit is caught); production sends
    # "drop_block" and alerts on the reported drops (PLAN.md §5.4).
    thinking_prefix_mismatch: Literal["error", "drop_block"] = "error"

    # ---------------------------------------------------------------- evals (Phase 4)

    # The simulated customer (D2). Low effort: it plays a role from a short brief, and the agent
    # is what is being measured.
    sim_model: str = "claude-opus-5-5"
    sim_effort: Literal["low", "medium", "high", "xhigh", "max"] = "low"

    @field_validator("anthropic_api_key", "openai_api_key", mode="before")
    @classmethod
    def _blank_key_is_no_key(cls, value: object) -> object:
        """`ANTHROPIC_API_KEY=` in .env means "not set", not an empty key."""
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @model_validator(mode="after")
    def _no_dev_secrets_in_prod(self) -> Self:
        secret = self.mcp_jwt_secret
        if self.env == "prod" and secret and DEV_SECRET_MARKER in secret.get_secret_value():
            raise ValueError("RELAY_MCP_JWT_SECRET still has the development value")
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings, read once from the environment."""
    return Settings()
