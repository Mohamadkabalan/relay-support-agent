"""FastAPI application factory.

Every route lives under `/api`, because Caddy sends `/api/*` to this service and everything
else to the Next.js frontend.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from relay import __version__
from relay.api import health
from relay.api.resources import open_resources
from relay.config import Settings, get_settings


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with open_resources(resolved) as resources:
            app.state.resources = resources
            yield

    app = FastAPI(
        title="Relay API",
        version=__version__,
        lifespan=lifespan,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        redoc_url=None,
    )
    app.state.settings = resolved
    app.include_router(health.router, prefix="/api")
    return app
