from typer.testing import CliRunner

from relay import __version__
from relay.cli import app


def test_version_prints_the_package_version() -> None:
    result = CliRunner().invoke(app, ["version"])

    assert result.exit_code == 0
    assert result.output.strip() == __version__


def test_mcp_serve_rejects_an_unknown_transport() -> None:
    result = CliRunner().invoke(app, ["mcp", "serve", "--transport", "carrier-pigeon"])

    assert result.exit_code != 0
