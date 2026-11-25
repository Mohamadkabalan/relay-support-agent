import httpx2
from fastapi.testclient import TestClient
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from relay.config import Settings
from relay.mcp_server.server import build_server, create_http_app


async def test_in_memory_client_negotiates_the_current_protocol(offline_settings: Settings) -> None:
    async with Client(build_server(offline_settings), raise_exceptions=True) as client:
        assert client.protocol_version == "2026-07-28"
        result = await client.list_tools()

    assert result.tools == []  # the store tools arrive in Phase 2


async def test_streamable_http_round_trip_with_the_compose_hostname(
    offline_settings: Settings,
) -> None:
    app = create_http_app(offline_settings)
    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app)) as http_client,
        Client(streamable_http_client("http://mcp:8001/mcp", http_client=http_client)) as client,
    ):
        result = await client.list_tools()

    assert result.tools == []


def test_http_app_serves_healthz(offline_settings: Settings) -> None:
    with TestClient(create_http_app(offline_settings)) as client:
        response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_http_app_rejects_a_host_header_outside_the_allowlist(offline_settings: Settings) -> None:
    app = create_http_app(offline_settings)
    with TestClient(app, base_url="http://attacker.example") as client:
        response = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})

    assert response.status_code == 421  # DNS-rebinding protection
