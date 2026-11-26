"""The store tools on the test database, called through the in-memory MCP client (PLAN.md §6.1).

Tool calls run in their own sessions on the test's connection, so they see the rows the factory
flushed, and the test's rollback still removes everything they commit.
"""

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncConnection
from tests.support.factories import rollback_sessionmaker
from tests.support.mcp import Caller

from relay.clock import FrozenClock
from relay.domain.policy_config import PolicyConfig
from relay.domain.scope import CustomerScope
from relay.kb.embeddings import HashingEmbedder
from relay.mcp_server.identity import BoundIdentity
from relay.mcp_server.runtime import ToolRuntime
from relay.mcp_server.server import build_server


@pytest.fixture
def runtime(connection: AsyncConnection, clock: FrozenClock, policy: PolicyConfig) -> ToolRuntime:
    return ToolRuntime(
        sessionmaker=rollback_sessionmaker(connection),
        identity=BoundIdentity(CustomerScope(customer_id=uuid.uuid7())),
        embedder=HashingEmbedder(),
        clock=clock,
        policy=policy,
    )


@pytest.fixture
def caller(runtime: ToolRuntime) -> Caller:
    assert isinstance(runtime.identity, BoundIdentity)
    return Caller(build_server(runtime), runtime.identity)
