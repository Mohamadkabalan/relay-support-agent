"""Calling the MCP tools in tests the way a client does, through the in-memory transport."""

import json
from dataclasses import dataclass, replace
from typing import Any

from mcp import Client
from mcp_types import CallToolResult, TextContent

from relay.domain.scope import CustomerScope
from relay.mcp_server.identity import BoundIdentity
from relay.mcp_server.runtime import RelayServer


@dataclass
class Caller:
    """Calls tools as one customer, one turn at a time.

    Each call opens its own in-process client: cheap, and it keeps the client's task group inside
    the test's own task.
    """

    server: RelayServer
    identity: BoundIdentity

    def act_as(self, scope: CustomerScope) -> None:
        self.identity.customer = scope

    def next_turn(self) -> None:
        scope = self.identity.customer
        self.identity.customer = replace(scope, turn_seq=(scope.turn_seq or 0) + 1)

    async def call(self, tool: str, **arguments: Any) -> CallToolResult:
        async with Client(self.server) as client:
            return await client.call_tool(tool, arguments)

    async def ok(self, tool: str, **arguments: Any) -> dict[str, Any]:
        """The structured result of a call that must succeed."""
        result = await self.call(tool, **arguments)
        assert not result.is_error, text_of(result)
        assert result.structured_content is not None
        content = result.structured_content
        # The SDK wraps a union return type (the write tools) as {"result": ...}.
        outcome: dict[str, Any] = content["result"] if set(content) == {"result"} else content
        assert json.loads(text_of(result)) == outcome  # the model reads the same data as text
        return outcome

    async def fails(self, tool: str, **arguments: Any) -> str:
        """The error text of a call that must fail."""
        result = await self.call(tool, **arguments)
        assert result.is_error, result.structured_content
        return text_of(result)


def text_of(result: CallToolResult) -> str:
    block = result.content[0]
    assert isinstance(block, TextContent)
    return block.text
