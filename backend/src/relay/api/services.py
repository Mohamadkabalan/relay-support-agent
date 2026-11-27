"""Everything the API holds for its lifetime, built once by the app's lifespan.

The agent is optional: without a model key the API still serves logins, the console and
approvals, and the chat endpoints answer 503 with the reason. Tests pass `Overrides` to swap in
the scripted model, the MCP app in process, a frozen clock or tighter limits.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator, Coroutine
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from anthropic import AsyncAnthropic
from fastapi import HTTPException, Request, status
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from relay.agent.factory import MissingCredentialsError, build_agent, build_provider, http_tools
from relay.agent.locks import RedisLocks
from relay.agent.loop import Agent, AgentLimits
from relay.agent.providers.base import ModelProvider
from relay.agent.scheduler import Scheduler
from relay.agent.spend import RedisSpend, SpendGuard
from relay.agent.store import ConversationStore
from relay.agent.tools import ToolConnector
from relay.api.events import EventBus
from relay.api.ratelimit import RateLimiter
from relay.api.resources import Resources, open_resources
from relay.api.sessions import SessionStore
from relay.clock import Clock, SystemClock
from relay.config import Settings
from relay.db.session import make_sessionmaker
from relay.domain.policy_config import PolicyConfig, default_policy


@dataclass(frozen=True)
class Overrides:
    provider: ModelProvider | None = None
    tools: ToolConnector | None = None
    clock: Clock | None = None
    limits: AgentLimits | None = None
    sweep_every_s: float | None = 60.0  # None: no background sweep (tests call it themselves)
    drafter: AsyncAnthropic | None = None  # the client that drafts eval scenarios


@dataclass
class Services:
    settings: Settings
    resources: Resources
    sessionmaker: async_sessionmaker[AsyncSession]
    clock: Clock
    policy: PolicyConfig
    sessions: SessionStore
    limiter: RateLimiter
    bus: EventBus
    store: ConversationStore
    spend: SpendGuard | None
    agent: Agent | None
    scheduler: Scheduler | None
    agent_problem: str | None = None  # why there is no agent
    drafter: AsyncAnthropic | None = None  # None: drafting scenarios needs ANTHROPIC_API_KEY
    background: set[asyncio.Task[None]] = field(default_factory=set)

    def spawn(self, work: Coroutine[Any, Any, None]) -> None:
        """Run `work` in the background, keeping a reference until it finishes."""
        task = asyncio.create_task(work)
        self.background.add(task)
        task.add_done_callback(self.background.discard)

    @property
    def engine(self) -> AsyncEngine:
        return self.resources.engine

    @property
    def redis(self) -> Redis:
        return self.resources.redis

    def require_agent(self) -> tuple[Agent, Scheduler]:
        if self.agent is None or self.scheduler is None:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                f"The assistant is not available: {self.agent_problem}",
            )
        return self.agent, self.scheduler

    def kick(self, conversation_id: uuid.UUID) -> None:
        """Schedule a turn for whatever is now in the conversation's inbox, if there's an agent."""
        if self.scheduler is not None:
            self.scheduler.kick(conversation_id)


@asynccontextmanager
async def open_services(
    settings: Settings, overrides: Overrides | None = None
) -> AsyncIterator[Services]:
    overrides = overrides or Overrides()
    if settings.session_secret is None:
        raise RuntimeError("Set RELAY_SESSION_SECRET to serve the API (see .env.example).")
    prefix = settings.redis_prefix
    clock = overrides.clock or SystemClock()
    policy = default_policy()
    async with open_resources(settings) as resources:
        sessionmaker = make_sessionmaker(resources.engine)
        bus = EventBus(resources.redis, prefix)
        store = ConversationStore(sessionmaker, clock, policy)
        spend = None
        if settings.daily_spend_cap_usd is not None:
            spend = RedisSpend(resources.redis, settings.daily_spend_cap_usd, clock, prefix)
        agent, scheduler, problem = None, None, None
        try:
            provider = overrides.provider or build_provider(settings)
            tools = overrides.tools or http_tools(settings)
        except MissingCredentialsError as error:
            problem = str(error)
        else:
            agent = build_agent(
                settings,
                sessionmaker,
                provider=provider,
                tools=tools,
                clock=clock,
                policy=policy,
                locks=RedisLocks(resources.redis, prefix + "lock:"),
                limits=overrides.limits,
                listener=bus.publish,
                spend=spend,
                trace_content=settings.trace_content != "none",
            )
            scheduler = Scheduler(
                agent,
                sessionmaker,
                publish=bus.publish,
                idle_after=timedelta(minutes=settings.idle_close_minutes),
            )
        services = Services(
            settings=settings,
            resources=resources,
            sessionmaker=sessionmaker,
            clock=clock,
            policy=policy,
            sessions=SessionStore(
                resources.redis,
                settings.session_secret.get_secret_value(),
                settings.session_ttl_s,
                prefix,
            ),
            limiter=RateLimiter(resources.redis, prefix),
            bus=bus,
            store=store,
            spend=spend,
            agent=agent,
            scheduler=scheduler,
            agent_problem=problem,
            drafter=overrides.drafter or _drafter(settings),
        )
        if scheduler is not None and overrides.sweep_every_s is not None:
            scheduler.start_sweeping(overrides.sweep_every_s)
        try:
            yield services
        finally:
            if scheduler is not None:
                await scheduler.stop()
            for task in list(services.background):
                task.cancel()
            await asyncio.gather(*services.background, return_exceptions=True)


def _drafter(settings: Settings) -> AsyncAnthropic | None:
    key = settings.anthropic_api_key
    if key is None:
        return None
    return AsyncAnthropic(api_key=key.get_secret_value(), max_retries=2, timeout=300)


def get_services(request: Request) -> Services:
    services = getattr(request.app.state, "services", None)
    if not isinstance(services, Services):
        raise RuntimeError("API services are not initialized; is the lifespan running?")
    return services
