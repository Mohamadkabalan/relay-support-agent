"""The API under test: a real uvicorn server in this process, so SSE streams for real.

The model is a `ScriptedProvider`; the MCP server is the production app served in process over
an ASGI transport, with per-turn JWTs, on the same database. Redis keys get a prefix of their
own, so rate limits, sessions and locks never leak between tests. The clock is the test clock.
"""

import asyncio
import json
import os
import secrets
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import httpx2
import uvicorn
from anthropic import AsyncAnthropic
from fastapi import FastAPI
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from relay.agent.providers.base import ModelProvider
from relay.agent.providers.scripted import ScriptedProvider, Step
from relay.agent.tools import HttpTools
from relay.api.app import create_app
from relay.api.services import Overrides, Services
from relay.clock import FrozenClock
from relay.db.models import Customer
from relay.db.session import create_engine, make_sessionmaker
from relay.domain.policy_config import default_policy
from relay.kb.embeddings import HashingEmbedder
from relay.mcp_server.identity import TokenIdentity
from relay.mcp_server.runtime import ToolRuntime
from relay.mcp_server.server import create_http_app
from tests.conftest import TEST_JWT_SECRET, make_settings
from tests.support.factories import make_engine

ORIGIN = "http://localhost:8080"


@dataclass
class RunningApi:
    url: str
    app: FastAPI
    provider: ScriptedProvider

    @property
    def services(self) -> Services:
        services = self.app.state.services
        assert isinstance(services, Services)
        return services

    def client(self) -> httpx2.AsyncClient:
        """A browser: its own cookie jar, sending the allowed Origin."""
        return httpx2.AsyncClient(base_url=self.url, headers={"Origin": ORIGIN}, timeout=10)

    async def idle(self) -> None:
        """Wait for every scheduled turn to finish."""
        scheduler = self.services.scheduler
        assert scheduler is not None
        await asyncio.sleep(0)
        await scheduler.idle()


@asynccontextmanager
async def running_api(
    database_url: str,
    steps: Sequence[Step] | None = None,
    *,
    clock: FrozenClock,
    provider: ModelProvider | None = None,
    drafter: AsyncAnthropic | None = None,
    **settings: Any,
) -> AsyncIterator[RunningApi]:
    values: dict[str, Any] = {
        "database_url": database_url,
        "redis_url": os.environ["REDIS_URL"],
        "demo_mode": True,
        "cookie_secure": False,
        "allowed_origins": (ORIGIN,),
        "redis_prefix": f"relay-test-{secrets.token_hex(4)}:",
        **settings,
    }
    config = make_settings(**values)
    scripted = ScriptedProvider(list(steps or []))
    engine = create_engine(str(config.database_url), pool_size=4)
    runtime = ToolRuntime(
        sessionmaker=make_sessionmaker(engine),
        identity=TokenIdentity(),
        embedder=HashingEmbedder(),
        clock=clock,
        policy=default_policy(),
    )
    mcp_app = create_http_app(config, runtime)
    tools = HttpTools(
        "http://mcp:8001/mcp", TEST_JWT_SECRET, transport=httpx2.ASGITransport(app=mcp_app)
    )
    app = create_app(
        config,
        Overrides(
            provider=provider or scripted,
            tools=tools,
            clock=clock,
            sweep_every_s=None,
            drafter=drafter,
        ),
    )
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=0, log_config=None, lifespan="on")
    )
    try:
        async with mcp_app.router.lifespan_context(mcp_app):
            serving = asyncio.create_task(server.serve())
            async with asyncio.timeout(10):
                while not server.started:
                    if serving.done():
                        serving.result()  # raise the startup error
                    await asyncio.sleep(0.01)
            port = server.servers[0].sockets[0].getsockname()[1]
            try:
                yield RunningApi(f"http://127.0.0.1:{port}", app, scripted)
            finally:
                server.should_exit = True
                await serving
    finally:
        await engine.dispose()
        await _forget(config.redis_url, config.redis_prefix)


async def _forget(url: object, prefix: str) -> None:
    redis = Redis.from_url(str(url))
    try:
        keys = [key async for key in redis.scan_iter(match=f"{prefix}*")]
        if keys:
            await redis.delete(*keys)
    finally:
        await redis.aclose()


# ---------------------------------------------------------------- SSE


@dataclass
class Sse:
    event: str
    data: Any
    id: int | None


@dataclass
class EventStream:
    """Reads one open SSE response, event by event."""

    response: httpx2.Response
    seen: list[Sse] = field(default_factory=list)
    _lines: AsyncIterator[str] | None = None

    async def next(self) -> Sse:
        if self._lines is None:
            self._lines = self.response.aiter_lines()
        event, event_id = "message", None
        data: list[str] = []
        async for line in self._lines:
            if line == "":
                if data:
                    parsed = Sse(event, json.loads("\n".join(data)), event_id)
                    self.seen.append(parsed)
                    return parsed
                event, data, event_id = "message", [], None
                continue
            if line.startswith(":"):
                continue
            name, _, value = line.partition(":")
            value = value.removeprefix(" ")
            if name == "event":
                event = value
            elif name == "data":
                data.append(value)
            elif name == "id":
                event_id = int(value)
        raise EOFError("the stream ended")

    async def until(self, done: Callable[[Sse], bool], within_s: float = 10) -> list[Sse]:
        """Every event up to and including the first that satisfies `done`."""
        got: list[Sse] = []
        async with asyncio.timeout(within_s):
            while True:
                event = await self.next()
                got.append(event)
                if done(event):
                    return got


@asynccontextmanager
async def open_stream(
    client: httpx2.AsyncClient, path: str, last_event_id: int | None = None
) -> AsyncIterator[EventStream]:
    headers = {"Last-Event-ID": str(last_event_id)} if last_event_id is not None else {}
    async with client.stream("GET", path, headers=headers, timeout=None) as response:
        assert response.status_code == 200, response.status_code
        yield EventStream(response)


def turn_completed(event: Sse) -> bool:
    return event.event == "turn.completed"


async def customer_by_visitor_email(url: str, prefix: str) -> Customer:
    """The newest demo copy whose email starts with `prefix` (e.g. "dana.whitfield+")."""
    engine = make_engine(url)
    try:
        async with AsyncSession(engine) as session:
            found = await session.scalar(
                select(Customer)
                .where(Customer.email.startswith(prefix))
                .order_by(Customer.id.desc())
                .limit(1)
            )
    finally:
        await engine.dispose()
    assert found is not None
    return found
