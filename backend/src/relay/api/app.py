"""FastAPI application factory.

Every route lives under `/api`, because Caddy sends `/api/*` to this service and everything
else to the Next.js frontend.

- `create_app(settings, overrides)` builds the app; tests call it directly.
- `serve_app()` is what uvicorn runs: it also sets up JSON logging and tracing for the process.
"""

from collections.abc import AsyncIterator, MutableMapping
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from relay import __version__
from relay.api import health
from relay.api.middleware import BodyLimit, OriginCheck, RequestContext
from relay.api.routes import auth, chat, console
from relay.api.services import Overrides, open_services
from relay.config import Settings, get_settings
from relay.domain.errors import ConflictError, DomainError, NotFoundError
from relay.telemetry.logging import configure_logging
from relay.telemetry.tracing import setup_tracing

# The container healthcheck polls these every few seconds; a span each would bury the real traces.
UNTRACED_PATHS = frozenset({"/api/healthz", "/api/readyz"})


def untraced(scope: MutableMapping[str, Any]) -> bool:
    return scope.get("path") in UNTRACED_PATHS


def create_app(settings: Settings | None = None, overrides: Overrides | None = None) -> FastAPI:
    resolved = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with open_services(resolved, overrides) as services:
            app.state.services = services
            app.state.resources = services.resources
            yield

    app = FastAPI(
        title="Relay API",
        version=__version__,
        lifespan=lifespan,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        redoc_url=None,
        # FastAPI's native OpenTelemetry: one server span per request, named by route. Its
        # per-dependency spans, metrics and log export stay off; our own spans and JSON logs
        # cover those, and everything exported goes through the redacting exporter.
        telemetry={"operation_spans": False, "metrics": False, "logs": False, "exclude": untraced},
    )
    app.state.settings = resolved
    # Each router carries the full /api prefix itself, so a matched route's path is the whole
    # template (the request spans are named by it).
    for router in (health.router, auth.router, chat.router, console.router):
        app.include_router(router)

    @app.exception_handler(DomainError)
    async def domain_error(_request: Request, error: DomainError) -> JSONResponse:
        code = (
            404
            if isinstance(error, NotFoundError)
            else 409
            if isinstance(error, ConflictError)
            else 422
        )
        return JSONResponse({"detail": error.message, "code": error.code}, status_code=code)

    # Outermost first: request context (IDs, spans), then the origin check, then the body limit.
    app.add_middleware(BodyLimit, max_bytes=resolved.max_body_bytes)
    app.add_middleware(OriginCheck, allowed=resolved.allowed_origins)
    app.add_middleware(RequestContext)
    return app


def serve_app() -> FastAPI:
    """The uvicorn factory: JSON logs and tracing for this process, then the app."""
    settings = get_settings()
    configure_logging(settings)
    setup_tracing(settings, "relay-api")
    return create_app(settings)
