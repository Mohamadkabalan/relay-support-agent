"""Connections the API holds for its whole lifetime: the Postgres engine and the Redis client."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from fastapi import Request
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from relay.config import Settings
from relay.db.session import engine_from_settings


@dataclass(frozen=True)
class Resources:
    engine: AsyncEngine
    redis: Redis


@asynccontextmanager
async def open_resources(settings: Settings) -> AsyncIterator[Resources]:
    """Create the engine and the Redis client, and close both on exit.

    Neither connects here: the first query or command opens a connection, so a process can
    start (and report itself not ready) while Postgres or Redis is still coming up.
    """
    engine = engine_from_settings(settings)
    redis = Redis.from_url(str(settings.redis_url))
    try:
        yield Resources(engine=engine, redis=redis)
    finally:
        await redis.aclose()
        await engine.dispose()


def get_resources(request: Request) -> Resources:
    """FastAPI dependency: the resources opened by the app's lifespan."""
    resources = request.app.state.resources
    if not isinstance(resources, Resources):
        raise RuntimeError("API resources are not initialized; is the lifespan running?")
    return resources
