import pytest
from typer.testing import CliRunner

from relay import __version__
from relay.cli import app
from relay.config import get_settings


def test_version_prints_the_package_version() -> None:
    result = CliRunner().invoke(app, ["version"])

    assert result.exit_code == 0
    assert result.output.strip() == __version__


def test_mcp_serve_rejects_an_unknown_transport() -> None:
    result = CliRunner().invoke(app, ["mcp", "serve", "--transport", "carrier-pigeon"])

    assert result.exit_code != 0


def test_mcp_stdio_needs_a_customer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://relay:relay@127.0.0.1:1/relay")
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1/0")
    monkeypatch.delenv("RELAY_MCP_CUSTOMER", raising=False)
    get_settings.cache_clear()
    try:
        result = CliRunner().invoke(app, ["mcp", "serve", "--transport", "stdio"])
    finally:
        get_settings.cache_clear()

    assert result.exit_code == 1
    assert "Set RELAY_MCP_CUSTOMER" in result.output
