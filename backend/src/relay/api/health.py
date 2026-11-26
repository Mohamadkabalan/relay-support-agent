"""Liveness and readiness endpoints.

`/api/healthz` says the process is up. `/api/readyz` also checks that Postgres and Redis
answer; Docker Compose uses it as the container healthcheck.
"""

import asyncio
import logging
from collections.abc import Awaitable
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import BaseModel
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from relay.api.resources import Resources, get_resources
from relay.config import Settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["health"])

CheckResult = Literal["ok", "error"]


class Health(BaseModel):
    status: Literal["ok"]


class Readiness(BaseModel):
    status: Literal["ready", "not_ready"]
    checks: dict[str, CheckResult]


@router.get("/healthz")
async def healthz() -> Health:
    return Health(status="ok")


@router.get(
    "/readyz",
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"model": Readiness}},
)
async def readyz(
    request: Request,
    response: Response,
    resources: Annotated[Resources, Depends(get_resources)],
) -> Readiness:
    settings: Settings = request.app.state.settings

    async def probe(name: str, check: Awaitable[None]) -> CheckResult:
        try:
            async with asyncio.timeout(settings.readiness_timeout_s):
                await check
        except Exception as exc:  # any failure means "not ready"; the log says why
            logger.warning("readiness check %s failed: %s", name, type(exc).__name__)
            return "error"
        return "ok"

    postgres, redis = await asyncio.gather(
        probe("postgres", _ping_postgres(resources.engine)),
        probe("redis", _ping_redis(resources.redis)),
    )
    checks = {"postgres": postgres, "redis": redis}
    if all(result == "ok" for result in checks.values()):
        return Readiness(status="ready", checks=checks)
    response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return Readiness(status="not_ready", checks=checks)


async def _ping_postgres(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        await connection.execute(text("SELECT 1"))


async def _ping_redis(redis: Redis) -> None:
    if not await redis.ping():
        raise RuntimeError("Redis did not answer PING")
