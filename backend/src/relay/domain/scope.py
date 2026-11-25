"""Who is acting, for whom, and with what.

Identity never comes from tool arguments (ADR-0003). The MCP server builds a `CustomerScope` from
the verified per-turn token; the operator console builds an `OperatorScope` from the operator's
session. Every service reads the customer from the scope, so it can only reach that customer's data.
"""

import uuid
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession

from relay.clock import Clock
from relay.domain.enums import ActorType
from relay.domain.policy_config import PolicyConfig, default_policy


@dataclass(frozen=True, slots=True)
class Deps:
    """What every service needs. The caller owns the session's transaction."""

    session: AsyncSession
    clock: Clock
    policy: PolicyConfig = field(default_factory=default_policy)


@dataclass(frozen=True, slots=True)
class CustomerScope:
    """The agent (or Claude Desktop over stdio) acting for one signed-in customer."""

    customer_id: uuid.UUID
    conversation_id: uuid.UUID | None = None
    turn_seq: int | None = None  # None over stdio, where there are no turns
    trace_id: str | None = None

    @property
    def actor_type(self) -> ActorType:
        return ActorType.AGENT

    @property
    def actor_id(self) -> str:
        return f"conversation:{self.conversation_id}" if self.conversation_id else "stdio"


@dataclass(frozen=True, slots=True)
class OperatorScope:
    operator_id: uuid.UUID
    trace_id: str | None = None

    @property
    def actor_type(self) -> ActorType:
        return ActorType.OPERATOR

    @property
    def actor_id(self) -> str:
        return str(self.operator_id)


@dataclass(frozen=True, slots=True)
class SystemScope:
    """Scheduled jobs, such as approval expiry."""

    job: str
    trace_id: str | None = None

    @property
    def actor_type(self) -> ActorType:
        return ActorType.SYSTEM

    @property
    def actor_id(self) -> str:
        return self.job


Scope = CustomerScope | OperatorScope | SystemScope
