"""Shared fixtures.

Unit and MCP tests need nothing running. Integration tests need Postgres and Redis: `make test`
starts them and passes DATABASE_URL and REDIS_URL; CI provides them as service containers.
"""

import os

import pytest

from relay.config import Settings

# Port 1 refuses connections immediately, which makes "dependency is down" tests fast.
UNREACHABLE_DATABASE_URL = "postgresql+asyncpg://relay:relay@127.0.0.1:1/relay"
UNREACHABLE_REDIS_URL = "redis://127.0.0.1:1/0"
TEST_JWT_SECRET = "test-only-secret-for-turn-tokens-0123456789"
TEST_SESSION_SECRET = "test-only-secret-for-session-ids-0123456789"


def make_settings(**overrides: object) -> Settings:
    """Build Settings from explicit values only, ignoring the process environment."""
    values: dict[str, object] = {
        "env": "test",
        "database_url": UNREACHABLE_DATABASE_URL,
        "redis_url": UNREACHABLE_REDIS_URL,
        "mcp_jwt_secret": TEST_JWT_SECRET,
        "session_secret": TEST_SESSION_SECRET,
        **overrides,
    }
    return Settings.model_validate(values)


@pytest.fixture
def offline_settings() -> Settings:
    """Settings whose Postgres and Redis cannot be reached."""
    return make_settings()


@pytest.fixture
def live_settings() -> Settings:
    """Settings pointing at the real Postgres and Redis given in the environment."""
    missing = [name for name in ("DATABASE_URL", "REDIS_URL") if not os.environ.get(name)]
    if missing:
        pytest.fail(
            f"Integration tests need {' and '.join(missing)}. Run them with `make test`, "
            "which starts Postgres and Redis and sets both."
        )
    return make_settings(
        database_url=os.environ["DATABASE_URL"],
        redis_url=os.environ["REDIS_URL"],
    )
