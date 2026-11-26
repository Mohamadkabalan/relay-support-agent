"""What the tools run with, and how their failures reach the model.

`RelayServer` is the SDK's `MCPServer` plus two things:

- a `ToolRuntime` (database, clock, policy, embedder, identity), which tools reach through their
  `Context`, so tests can swap any of it;
- error messages in the `[CODE] message hint` form (§6.2). The SDK reports a failed call as
  `is_error: true` with "Error executing tool <name>: ..." and Pydantic's raw text for bad
  arguments. We replace that with the domain error's own text, a short `[INVALID_ARGUMENT]`
  summary that never echoes the input back, or a generic `[INTERNAL]` message for a crash, whose
  details stay in the server log.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError
from mcp_types import CallToolResult, InputRequiredResult
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from relay.clock import Clock, SystemClock
from relay.domain.errors import DomainError
from relay.domain.policy_config import PolicyConfig, default_policy
from relay.domain.scope import CustomerScope, Deps
from relay.kb.embeddings import Embedder
from relay.mcp_server.identity import Identity

logger = logging.getLogger(__name__)

INTERNAL_ERROR = (
    "[INTERNAL] Something went wrong on our side. Try the same call once more; if it fails "
    "again, apologize and call escalate_to_human."
)


@dataclass
class ToolRuntime:
    sessionmaker: async_sessionmaker[AsyncSession]
    identity: Identity
    embedder: Embedder
    clock: Clock = field(default_factory=SystemClock)
    policy: PolicyConfig = field(default_factory=default_policy)


class RelayServer(MCPServer[Any]):
    def __init__(self, runtime: ToolRuntime, **options: Any) -> None:
        super().__init__(**options)
        self.runtime = runtime

    async def call_tool(
        self, name: str, arguments: dict[str, Any], context: Context[Any, Any] | None = None
    ) -> CallToolResult | InputRequiredResult:
        try:
            return await super().call_tool(name, arguments, context)
        except ToolError as error:
            raise ToolError(public_message(name, error)) from error


def public_message(tool: str, error: ToolError) -> str:
    """The text the model sees for a failed call."""
    cause = error.__cause__
    if isinstance(cause, DomainError):
        return str(cause)
    if isinstance(cause, ValidationError):
        return invalid_arguments(cause)
    if isinstance(cause, ToolError):  # raised on purpose by a tool or resolver, already coded
        return str(cause)
    if isinstance(error, UnexpectedToolError):
        logger.error("Tool %s crashed", tool, exc_info=cause)
        return INTERNAL_ERROR
    return f"[INVALID_ARGUMENT] {error}"  # for example an unknown tool name


def invalid_arguments(error: ValidationError) -> str:
    problems = []
    for item in error.errors(include_url=False, include_input=False):
        where = ".".join(str(part) for part in item["loc"]) or "arguments"
        problems.append(f"{where}: {item['msg'].removeprefix('Value error, ')}")
    return f"[INVALID_ARGUMENT] {'; '.join(problems)}. Fix the arguments and call the tool again."


def runtime_of(ctx: Context[Any, Any]) -> ToolRuntime:
    server = ctx.mcp_server
    if not isinstance(server, RelayServer):
        raise TypeError("Relay tools run only on a RelayServer")
    return server.runtime


def current_customer(ctx: Context[Any, Any]) -> CustomerScope:
    """Resolver for every tool's `customer` parameter, which never appears in the tool schema."""
    return runtime_of(ctx).identity.scope()


@asynccontextmanager
async def transaction(ctx: Context[Any, Any]) -> AsyncIterator[Deps]:
    """One database transaction per tool call: committed on success, rolled back on any error."""
    runtime = runtime_of(ctx)
    async with runtime.sessionmaker() as session, session.begin():
        yield Deps(session=session, clock=runtime.clock, policy=runtime.policy)
