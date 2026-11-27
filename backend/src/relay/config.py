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
    # "scripted" is the key-free demo agent (relay/agent/providers/demo.py): no model is called.
    provider: Literal["anthropic", "openai", "scripted"] = "anthropic"
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
    # Drafts eval scenarios from conversations in the console (§10).
    drafter_model: str = "claude-opus-5-5"

    # ---------------------------------------------------------------- the API (Phase 5)

    # Keys session IDs before they are used as Redis keys, so a Redis dump holds no usable
    # session (32+ characters). Required to serve the API.
    session_secret: SecretStr | None = Field(default=None, min_length=32)
    session_ttl_s: int = Field(default=12 * 3600, gt=0)
    # Secure cookies need HTTPS; the local stack on http://localhost:8080 turns this off.
    cookie_secure: bool = True
    # State-changing requests from a browser must come from one of these origins (§11).
    allowed_origins: tuple[str, ...] = ("http://localhost:8080",)
    # Every Redis key and channel the API uses starts with this, so tests can't collide.
    redis_prefix: str = "relay:"

    # Demo mode (§12): the persona picker gives each visitor private copies of the personas
    # (D4), and a demo operator login sees only that visitor's conversations.
    demo_mode: bool = False
    # The operator login outside demo mode: one operator with an Argon2 password hash
    # (`relay operator hash-password`).
    operator_email: str = "operator@relay.example"
    operator_password_hash: SecretStr | None = None

    # Limits (§5.7, §15). Messages count per session; chat requests and logins per client IP.
    max_body_bytes: int = Field(default=16 * 1024, gt=0)
    max_message_chars: int = Field(default=2000, gt=0)
    messages_per_minute: int = Field(default=8, gt=0)
    messages_per_day: int = Field(default=120, gt=0)
    chat_requests_per_minute: int = Field(default=30, gt=0)
    logins_per_minute: int = Field(default=10, gt=0)
    operator_requests_per_minute: int = Field(default=120, gt=0)
    # A conversation with no activity for this long is closed (§5.3).
    idle_close_minutes: int = Field(default=30, gt=0)

    # The hard daily spend cap (§12): a model call starts only if its worst-case cost still fits
    # under it. None turns it off (the evals and `relay chat` never use it).
    daily_spend_cap_usd: float | None = Field(default=20.0, ge=0)

    # Tracing (§10). Spans go to OTLP/HTTP at this endpoint (Phoenix: http://phoenix:6006); unset
    # means no export. `trace_content`: redacted message previews, none at all, or full (local
    # debugging only).
    otel_endpoint: str | None = Field(
        default=None,
        validation_alias=AliasChoices("OTEL_EXPORTER_OTLP_ENDPOINT", "otel_endpoint"),
    )
    trace_content: Literal["redacted", "none", "full"] = "redacted"
    # Where an operator's browser reaches Phoenix, for the console's trace links. None: no links.
    phoenix_url: str | None = "http://localhost:6006"

    @field_validator("anthropic_api_key", "openai_api_key", mode="before")
    @classmethod
    def _blank_key_is_no_key(cls, value: object) -> object:
        """`ANTHROPIC_API_KEY=` in .env means "not set", not an empty key."""
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("operator_password_hash", "otel_endpoint", "phoenix_url", mode="before")
    @classmethod
    def _blank_is_unset(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @model_validator(mode="after")
    def _no_dev_secrets_in_prod(self) -> Self:
        if self.env != "prod":
            return self
        for name, secret in (
            ("RELAY_MCP_JWT_SECRET", self.mcp_jwt_secret),
            ("RELAY_SESSION_SECRET", self.session_secret),
        ):
            if secret and DEV_SECRET_MARKER in secret.get_secret_value():
                raise ValueError(f"{name} still has the development value")
        if not self.cookie_secure:
            raise ValueError("RELAY_COOKIE_SECURE must stay on in production")
        if self.trace_content == "full":
            raise ValueError("RELAY_TRACE_CONTENT=full would export unredacted text")
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings, read once from the environment."""
    return Settings()
