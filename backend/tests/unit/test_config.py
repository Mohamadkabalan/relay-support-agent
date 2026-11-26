import pytest
from pydantic import ValidationError

from relay.config import Settings

DB_URL = "postgresql+asyncpg://relay:secret@db.internal:5432/relay"
REDIS_URL = "redis://cache.internal:6379/0"


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """Remove every variable Settings reads, so the developer's shell can't leak in."""
    for name in ("DATABASE_URL", "REDIS_URL", "RELAY_DATABASE_URL", "RELAY_REDIS_URL", "RELAY_ENV"):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_reads_urls_and_prefixed_settings_from_the_environment(
    clean_env: pytest.MonkeyPatch,
) -> None:
    clean_env.setenv("DATABASE_URL", DB_URL)
    clean_env.setenv("REDIS_URL", REDIS_URL)
    clean_env.setenv("RELAY_ENV", "prod")

    settings = Settings()

    assert settings.env == "prod"
    assert str(settings.database_url) == DB_URL
    assert settings.redis_url.host == "cache.internal"


def test_relay_prefixed_url_names_also_work(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("RELAY_DATABASE_URL", DB_URL)
    clean_env.setenv("RELAY_REDIS_URL", REDIS_URL)

    assert str(Settings().database_url) == DB_URL


def test_missing_database_url_fails_fast(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("REDIS_URL", REDIS_URL)

    with pytest.raises(ValidationError, match="DATABASE_URL"):
        Settings()


def test_rejects_a_database_url_that_is_not_postgres(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("DATABASE_URL", "mysql://relay:secret@db.internal:3306/relay")
    clean_env.setenv("REDIS_URL", REDIS_URL)

    with pytest.raises(ValidationError, match="DATABASE_URL"):
        Settings()


def test_rejects_an_unknown_environment_name(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("DATABASE_URL", DB_URL)
    clean_env.setenv("REDIS_URL", REDIS_URL)
    clean_env.setenv("RELAY_ENV", "staging")

    with pytest.raises(ValidationError, match="env"):
        Settings()


def test_mcp_allowed_hosts_can_be_set_as_a_json_list(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("DATABASE_URL", DB_URL)
    clean_env.setenv("REDIS_URL", REDIS_URL)
    clean_env.setenv("RELAY_MCP_ALLOWED_HOSTS", '["mcp.internal", "mcp.internal:*"]')

    assert Settings().mcp_allowed_hosts == ("mcp.internal", "mcp.internal:*")


def test_a_blank_model_key_means_no_key() -> None:
    from tests.conftest import make_settings

    assert make_settings(anthropic_api_key="", openai_api_key="  ").anthropic_api_key is None
    assert make_settings(anthropic_api_key="  ").anthropic_api_key is None
    key = make_settings(anthropic_api_key="sk-ant-test").anthropic_api_key
    assert key is not None
    assert key.get_secret_value() == "sk-ant-test"
