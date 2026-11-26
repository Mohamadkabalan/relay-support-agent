"""The tools over Streamable HTTP with real per-turn tokens: identity comes only from the token."""

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp_types import CallToolResult
from tests.conftest import TEST_JWT_SECRET
from tests.support.factories import Factory
from tests.support.mcp import text_of

from relay import turn_tokens
from relay.config import Settings
from relay.db.models import Customer
from relay.mcp_server.identity import TokenIdentity
from relay.mcp_server.runtime import ToolRuntime
from relay.mcp_server.server import create_http_app


@asynccontextmanager
async def mcp_over_http(settings: Settings, runtime: ToolRuntime) -> AsyncIterator[Any]:
    """Yields `call(token, tool, **arguments)` against the ASGI app, as the agent will call it."""
    runtime.identity = TokenIdentity()
    app = create_http_app(settings, runtime)

    async def call(token: str, tool: str, **arguments: Any) -> CallToolResult:
        async with (
            httpx2.AsyncClient(
                transport=httpx2.ASGITransport(app=app),
                headers={"Authorization": f"Bearer {token}"},
            ) as http_client,
            Client(
                streamable_http_client("http://mcp:8001/mcp", http_client=http_client)
            ) as client,
        ):
            return await client.call_tool(tool, arguments)

    async with app.router.lifespan_context(app):
        yield call


def turn(customer: Customer, conversation: uuid.UUID, seq: int) -> str:
    return turn_tokens.mint(
        TEST_JWT_SECRET,
        customer_id=customer.id,
        conversation_id=conversation,
        turn_seq=seq,
        now=datetime.now(UTC),
    )


async def test_the_token_decides_whose_data_a_tool_sees(
    offline_settings: Settings, runtime: ToolRuntime, factory: Factory
) -> None:
    alice, bob = await factory.customer("Alice"), await factory.customer("Bob")
    bobs_order = await factory.order(bob, "delivered")
    alice_token = turn(alice, uuid.uuid7(), 1)

    async with mcp_over_http(offline_settings, runtime) as call:
        profile = await call(alice_token, "get_customer")
        probe = await call(alice_token, "get_order", order_number=bobs_order.number)
        bob_reads_it = await call(
            turn(bob, uuid.uuid7(), 1), "get_order", order_number=bobs_order.number
        )

    assert profile.structured_content is not None
    assert profile.structured_content["full_name"] == "Alice"
    assert probe.is_error
    assert text_of(probe).startswith(f"[ORDER_NOT_FOUND] No order {bobs_order.number}")
    assert not bob_reads_it.is_error


async def test_a_commit_needs_a_token_from_a_later_turn(
    offline_settings: Settings, runtime: ToolRuntime, factory: Factory
) -> None:
    customer = await factory.customer()
    order = await factory.order(customer, "delivered", lines=[("CMP-MOUSE", 1)])
    refund = {
        "order_number": order.number,
        "reason": "damaged_or_defective",
        "lines": [{"line": 1, "quantity": 1}],
    }
    conversation = uuid.uuid7()

    async with mcp_over_http(offline_settings, runtime) as call:
        preview = await call(turn(customer, conversation, 1), "issue_refund", **refund)
        assert preview.structured_content is not None
        token = preview.structured_content["result"]["confirmation_token"]
        same_turn = await call(
            turn(customer, conversation, 1), "issue_refund", **refund, confirmation_token=token
        )
        other_conversation = await call(
            turn(customer, uuid.uuid7(), 2), "issue_refund", **refund, confirmation_token=token
        )
        next_turn = await call(
            turn(customer, conversation, 2), "issue_refund", **refund, confirmation_token=token
        )

    assert text_of(same_turn).startswith("[CONFIRMATION_TOO_EARLY]")
    assert text_of(other_conversation).startswith("[CONFIRMATION_INVALID] This confirmation token")
    assert next_turn.structured_content is not None
    assert next_turn.structured_content["result"]["status"] == "completed"
