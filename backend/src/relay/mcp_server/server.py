"""The Relay MCP server.

Phase 0 skeleton: the server starts, negotiates the protocol and lists no tools. The store
tools, identity resolvers and JWT verification arrive in Phase 2 (PLAN.md §6).
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from relay import __version__
from relay.config import Settings, get_settings

SERVER_NAME = "relay"


def build_server(settings: Settings) -> MCPServer:
    """Create the MCP server object. Tools are registered on it in Phase 2."""
    return MCPServer(
        name=SERVER_NAME,
        version=__version__,
        instructions=f"Customer-support tools for {settings.store_name}.",
    )


def create_http_app(settings: Settings | None = None) -> Starlette:
    """Serve the MCP server over Streamable HTTP at /mcp, plus a /healthz route.

    The host app owns the lifespan: a mounted app's own lifespan never runs, so the session
    manager is started here (MCP SDK docs, "Add to an existing app").
    """
    resolved = settings or get_settings()
    server = build_server(resolved)
    mcp_app = server.streamable_http_app(
        transport_security=TransportSecuritySettings(
            allowed_hosts=list(resolved.mcp_allowed_hosts),
            allowed_origins=[],  # no browser should ever call this server
        ),
    )

    @asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        async with server.session_manager.run():
            yield

    async def healthz(_request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    return Starlette(
        routes=[
            Route("/healthz", healthz, methods=["GET"]),
            Mount("/", app=mcp_app),  # must come last: it matches every path
        ],
        lifespan=lifespan,
    )
