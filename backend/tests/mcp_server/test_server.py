"""The MCP server without a database: protocol, HTTP transport, token checks and error messages."""

import logging
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import httpx2
import jwt
import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from sqlalchemy.ext.asyncio import create_async_engine
from tests.conftest import TEST_JWT_SECRET, UNREACHABLE_DATABASE_URL

from relay import turn_tokens
from relay.config import Settings
from relay.db.session import make_sessionmaker
from relay.domain.scope import CustomerScope
from relay.kb.embeddings import HashingEmbedder
from relay.mcp_server.identity import BoundIdentity, TokenIdentity
from relay.mcp_server.runtime import INTERNAL_ERROR, ToolRuntime
from relay.mcp_server.server import build_server, create_http_app

TOOL_NAMES = [
    "get_customer",
    "list_orders",
    "get_order",
    "track_shipment",
    "search_help_articles",
    "update_shipping_address",
    "create_return",
    "issue_refund",
    "escalate_to_human",
]
LIST_TOOLS = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}


@pytest_asyncio.fixture
async def offline_runtime() -> AsyncIterator[ToolRuntime]:
    """A runtime whose database refuses connections: fine for anything that never queries."""
    engine = create_async_engine(UNREACHABLE_DATABASE_URL)
    yield ToolRuntime(
        sessionmaker=make_sessionmaker(engine),
        identity=BoundIdentity(CustomerScope(customer_id=uuid.uuid7())),
        embedder=HashingEmbedder(),
    )
    await engine.dispose()


def token(**overrides: object) -> str:
    now = datetime.now(UTC)
    claims: dict[str, object] = {
        "iss": turn_tokens.ISSUER,
        "aud": turn_tokens.AUDIENCE,
        "sub": str(uuid.uuid7()),
        "cid": str(uuid.uuid7()),
        "tseq": 1,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=5)).timestamp()),
        **overrides,
    }
    secret = str(claims.pop("secret", TEST_JWT_SECRET))
    return jwt.encode({k: v for k, v in claims.items() if v is not None}, secret, "HS256")


# ---------------------------------------------------------------- protocol


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_lists_the_nine_tools_in_catalog_order(
    offline_runtime: ToolRuntime, mode: str
) -> None:
    async with Client(build_server(offline_runtime), mode=mode) as client:
        expected = "2026-07-28" if mode == "auto" else client.protocol_version
        assert client.protocol_version == expected
        listed = await client.list_tools()

    assert [tool.name for tool in listed.tools] == TOOL_NAMES


async def test_a_malformed_argument_is_an_invalid_argument_error(
    offline_runtime: ToolRuntime,
) -> None:
    async with Client(build_server(offline_runtime)) as client:
        result = await client.call_tool("get_order", {"order_number": "order #12"})

    assert result.is_error
    assert result.content[0].text == (  # type: ignore[union-attr]
        "[INVALID_ARGUMENT] order_number: order numbers look like PV-104233. "
        "Fix the arguments and call the tool again."
    )


async def test_validation_errors_never_echo_the_input(offline_runtime: ToolRuntime) -> None:
    async with Client(build_server(offline_runtime)) as client:
        result = await client.call_tool("search_help_articles", {"query": "x" * 301})

    text = result.content[0].text  # type: ignore[union-attr]
    assert text.startswith("[INVALID_ARGUMENT] query: String should have at most 300 characters")
    assert "xxx" not in text
    assert "pydantic" not in text


async def test_an_unknown_tool_is_an_invalid_argument_error(offline_runtime: ToolRuntime) -> None:
    async with Client(build_server(offline_runtime)) as client:
        result = await client.call_tool("drop_tables", {})

    assert result.is_error
    assert result.content[0].text == "[INVALID_ARGUMENT] Unknown tool: drop_tables"  # type: ignore[union-attr]


async def test_a_crash_is_reported_as_internal_and_logged(
    offline_runtime: ToolRuntime, caplog: pytest.LogCaptureFixture
) -> None:
    async with Client(build_server(offline_runtime)) as client:
        result = await client.call_tool("get_customer", {})  # the database is unreachable

    assert result.is_error
    assert result.content[0].text == INTERNAL_ERROR  # type: ignore[union-attr]
    crash = next(r for r in caplog.records if r.message == "Tool get_customer crashed")
    assert crash.levelno == logging.ERROR
    assert crash.exc_info is not None  # the traceback stays in the server log


# ---------------------------------------------------------------- HTTP


@pytest.fixture
def http_runtime(offline_runtime: ToolRuntime) -> ToolRuntime:
    offline_runtime.identity = TokenIdentity()
    return offline_runtime


async def test_a_valid_turn_token_reaches_the_tools(
    offline_settings: Settings, http_runtime: ToolRuntime
) -> None:
    app = create_http_app(offline_settings, http_runtime)
    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app),
            headers={"Authorization": f"Bearer {token()}"},
        ) as http_client,
        Client(streamable_http_client("http://mcp:8001/mcp", http_client=http_client)) as client,
    ):
        listed = await client.list_tools()

    assert [tool.name for tool in listed.tools] == TOOL_NAMES


@pytest.mark.parametrize(
    ("case", "authorization"),
    [
        ("no token", None),
        ("not a JWT", "Bearer not-a-jwt"),
        ("expired", f"Bearer {token(exp=int(datetime.now(UTC).timestamp()) - 60)}"),
        ("wrong audience", f"Bearer {token(aud='some-other-service')}"),
        ("wrong issuer", f"Bearer {token(iss='someone-else')}"),
        ("bad signature", f"Bearer {token(secret='an-attacker-guessed-this-secret-value-12')}"),
        ("no conversation", f"Bearer {token(cid=None)}"),
        ("turn zero", f"Bearer {token(tseq=0)}"),
        ("unsigned", "Bearer " + jwt.encode({"sub": "x"}, key="", algorithm="none")),
    ],
)
def test_requests_without_a_valid_turn_token_get_401(
    offline_settings: Settings, http_runtime: ToolRuntime, case: str, authorization: str | None
) -> None:
    headers = {"Authorization": authorization} if authorization else {}
    with TestClient(
        create_http_app(offline_settings, http_runtime), base_url="http://mcp:8001"
    ) as client:
        response = client.post("/mcp", json=LIST_TOOLS, headers=headers)

    assert response.status_code == 401, case
    assert response.headers["www-authenticate"].startswith('Bearer error="invalid_token"')


def test_http_app_serves_healthz_without_a_token(
    offline_settings: Settings, http_runtime: ToolRuntime
) -> None:
    with TestClient(create_http_app(offline_settings, http_runtime)) as client:
        response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_http_app_rejects_a_host_header_outside_the_allowlist(
    offline_settings: Settings, http_runtime: ToolRuntime
) -> None:
    app = create_http_app(offline_settings, http_runtime)
    with TestClient(app, base_url="http://attacker.example") as client:
        response = client.post(
            "/mcp", json=LIST_TOOLS, headers={"Authorization": f"Bearer {token()}"}
        )

    assert response.status_code == 421  # DNS-rebinding protection


def test_http_app_rejects_an_oversized_request(
    offline_settings: Settings, http_runtime: ToolRuntime
) -> None:
    body = {**LIST_TOOLS, "params": {"padding": "x" * (64 * 1024)}}
    with TestClient(
        create_http_app(offline_settings, http_runtime), base_url="http://mcp"
    ) as client:
        response = client.post("/mcp", json=body, headers={"Authorization": f"Bearer {token()}"})

    assert response.status_code == 413


def test_http_app_builds_its_own_runtime_from_settings(offline_settings: Settings) -> None:
    app = create_http_app(offline_settings)  # connects lazily, so nothing is reached yet

    assert [type(route).__name__ for route in app.routes] == ["Route", "Mount"]


def test_http_needs_the_jwt_secret(offline_settings: Settings) -> None:
    settings = offline_settings.model_copy(update={"mcp_jwt_secret": None})
    with pytest.raises(RuntimeError, match="RELAY_MCP_JWT_SECRET"):
        create_http_app(settings)
