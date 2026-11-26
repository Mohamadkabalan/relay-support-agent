"""The production path: the agent calls the MCP server over Streamable HTTP with a JWT per turn.

The token carries the turn number, so the server itself refuses a commit in the turn of its
preview and accepts it in the next one; the agent can't get around that.
"""

import uuid

import httpx2
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection
from tests.conftest import TEST_JWT_SECRET
from tests.integration.agent.conftest import MakeHarness
from tests.support.factories import Factory, rollback_sessionmaker

from relay.agent.providers.scripted import Call, Reply, ScriptedRequest
from relay.agent.tools import HttpTools
from relay.clock import FrozenClock
from relay.config import Settings
from relay.db.models import Refund
from relay.domain.enums import ToolCallOutcome
from relay.domain.policy_config import PolicyConfig
from relay.kb.embeddings import HashingEmbedder
from relay.mcp_server.identity import TokenIdentity
from relay.mcp_server.runtime import ToolRuntime
from relay.mcp_server.server import create_http_app


async def test_confirm_then_commit_over_http_with_turn_tokens(
    make_harness: MakeHarness,
    factory: Factory,
    offline_settings: Settings,
    connection: AsyncConnection,
    clock: FrozenClock,
    policy: PolicyConfig,
) -> None:
    customer = await factory.customer()
    order = await factory.order(customer, lines=(("AUD-BUDSL", 1),), delivered_days_ago=3)
    runtime = ToolRuntime(
        sessionmaker=rollback_sessionmaker(connection),
        identity=TokenIdentity(),
        embedder=HashingEmbedder(),
        clock=clock,
        policy=policy,
    )
    app = create_http_app(offline_settings, runtime)
    tools = HttpTools(
        "http://mcp:8001/mcp", TEST_JWT_SECRET, transport=httpx2.ASGITransport(app=app)
    )
    refund = {
        "order_number": order.number,
        "reason": "damaged_or_defective",
        "lines": [{"line": 1, "quantity": 1}],
    }

    def with_token(sent: ScriptedRequest) -> Reply:
        token = sent.last_tool_result["confirmation_token"]
        return Reply(calls=(Call("issue_refund", {**refund, "confirmation_token": token}),))

    harness = make_harness(
        [
            Reply(calls=(Call("issue_refund", refund),)),
            with_token,  # same turn: the server says it's too early
            Reply(text="Shall I go ahead with the $59.99 refund?"),
            with_token,  # next turn
            Reply(text="Done."),
        ],
        tools=tools,
    )
    async with app.router.lifespan_context(app):
        conversation_id = await harness.start(customer.id)
        await harness.say(conversation_id, "Refund my broken earbuds, please.")
        await harness.say(conversation_id, "Yes.")

    calls = await harness.tool_calls(conversation_id)
    assert [(c.outcome, c.error_code) for c in calls] == [
        (ToolCallOutcome.OK, None),
        (ToolCallOutcome.ERROR, "CONFIRMATION_TOO_EARLY"),
        (ToolCallOutcome.OK, None),
    ]
    refunds = list(await harness.session.scalars(select(Refund).where(Refund.order_id == order.id)))
    assert len(refunds) == 1
    assert refunds[0].conversation_id == conversation_id
    assert isinstance(conversation_id, uuid.UUID)
