"""The agent loop on the test database, with the real MCP tools in process and a scripted model.

Everything shares the test's connection (see `rollback_sessionmaker`), so the agent sees the
factory's rows and the test's rollback removes whatever the agent wrote. Scripts make at most one
database-backed tool call per model response: parallel calls would share that one connection.
"""

import uuid
from collections.abc import Callable

import pytest
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession
from tests.support.agent import EventLog, Harness
from tests.support.factories import rollback_sessionmaker

from relay.agent.locks import LocalLocks
from relay.agent.loop import Agent, AgentLimits
from relay.agent.providers.base import ModelProvider
from relay.agent.providers.scripted import ScriptedProvider, Step
from relay.agent.store import ConversationStore
from relay.agent.tools import InProcessTools, ToolConnector
from relay.clock import FrozenClock
from relay.domain.policy_config import PolicyConfig
from relay.domain.scope import CustomerScope
from relay.kb.embeddings import HashingEmbedder
from relay.mcp_server.identity import BoundIdentity
from relay.mcp_server.runtime import ToolRuntime

MakeHarness = Callable[..., Harness]


@pytest.fixture
def in_process(
    connection: AsyncConnection, clock: FrozenClock, policy: PolicyConfig
) -> InProcessTools:
    runtime = ToolRuntime(
        sessionmaker=rollback_sessionmaker(connection),
        identity=BoundIdentity(CustomerScope(customer_id=uuid.UUID(int=0))),
        embedder=HashingEmbedder(),
        clock=clock,
        policy=policy,
    )
    return InProcessTools(runtime)


@pytest.fixture
def make_harness(
    connection: AsyncConnection,
    session: AsyncSession,
    clock: FrozenClock,
    policy: PolicyConfig,
    in_process: InProcessTools,
) -> MakeHarness:
    def make(
        steps: list[Step] | None = None,
        *,
        limits: AgentLimits | None = None,
        provider: ModelProvider | None = None,
        tools: ToolConnector | None = None,
    ) -> Harness:
        scripted = ScriptedProvider(steps or [])
        log = EventLog()
        agent = Agent(
            store=ConversationStore(rollback_sessionmaker(connection), clock, policy),
            provider=provider or scripted,
            tools=tools or in_process,
            locks=LocalLocks(),
            clock=clock,
            policy=policy,
            limits=limits or AgentLimits(),
            user_key_secret="test-user-key-secret",
            listener=log,
        )
        return Harness(agent, scripted, log, session)

    return make
