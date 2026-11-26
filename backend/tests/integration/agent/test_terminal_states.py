"""Every way a turn can end (PLAN.md §5.2), and that the transcript stays valid after each."""

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import Any

from sqlalchemy import select
from tests.integration.agent.conftest import MakeHarness
from tests.support.agent import Harness
from tests.support.factories import Factory

from relay.agent.loop import AgentLimits
from relay.agent.manifest import Manifest, ToolDef
from relay.agent.providers.base import EventSink, ModelRequest, ModelResponse, Stop, Usage
from relay.agent.providers.scripted import Call, Fail, Reply, ScriptedProvider, ScriptedRequest
from relay.agent.tools import ToolAnswer, ToolSession
from relay.db.models import Conversation, Escalation, Message, ModelCall, Turn
from relay.domain.enums import (
    ConversationStatus,
    EscalationReason,
    MessageKind,
    TurnStatus,
    TurnTrigger,
)
from relay.domain.scope import CustomerScope

K = MessageKind


async def turn_of(harness: Harness, turn_id: uuid.UUID) -> Turn:
    return await harness.session.get_one(Turn, turn_id)


async def test_step_limit_hands_off(make_harness: MakeHarness, factory: Factory) -> None:
    customer = await factory.customer()
    harness = make_harness(
        [Reply(calls=(Call("get_customer", {}),)), Reply(calls=(Call("list_orders", {}),))],
        limits=AgentLimits(max_steps=2),
    )
    conversation_id = await harness.start(customer.id)

    outcome = await harness.say(conversation_id, "Help")

    assert outcome.status is TurnStatus.STEP_LIMIT
    assert outcome.replies[-1].startswith("This needs one of our specialists")
    assert outcome.escalation_number is not None
    escalation = await harness.session.scalar(
        select(Escalation).where(Escalation.conversation_id == conversation_id)
    )
    assert escalation is not None
    assert escalation.reason is EscalationReason.COMPLEX_ISSUE
    conversation = await harness.session.get_one(Conversation, conversation_id)
    assert conversation.status is ConversationStatus.ESCALATED
    assert (await harness.kinds(conversation_id))[-2:] == [K.TOOL_RESULTS, K.ASSISTANT]
    assert (await turn_of(harness, outcome.turn_id)).steps == 2
    assert "conversation.escalated" in harness.log.types()
    harness.check_requests()


async def test_token_budget_warns_then_hands_off(
    make_harness: MakeHarness, factory: Factory
) -> None:
    customer = await factory.customer()
    heavy = Usage(input_tokens=4_400, output_tokens=100)

    def after_warning(sent: ScriptedRequest) -> Reply:
        assert sent.messages[-1]["role"] == "system"
        assert "close to its length limit" in sent.messages[-1]["content"]
        return Reply(calls=(Call("list_orders", {}),), usage=heavy)

    harness = make_harness(
        [Reply(calls=(Call("get_customer", {}),), usage=heavy), after_warning],
        limits=AgentLimits(conversation_tokens=5_000),
    )
    conversation_id = await harness.start(customer.id)

    outcome = await harness.say(conversation_id, "Help")

    assert outcome.status is TurnStatus.BUDGET_EXHAUSTED
    assert outcome.escalation_number is not None
    conversation = await harness.session.get_one(Conversation, conversation_id)
    assert conversation.budget_warned
    assert conversation.processed_tokens == 9_000
    harness.check_requests()


class SlowProvider(ScriptedProvider):
    async def complete(self, request: ModelRequest, on_event: EventSink) -> ModelResponse:
        await asyncio.sleep(5)
        return await super().complete(request, on_event)


async def test_turn_deadline_hands_off(make_harness: MakeHarness, factory: Factory) -> None:
    customer = await factory.customer()
    harness = make_harness(
        provider=SlowProvider([Reply(text="too late")]),
        limits=AgentLimits(turn_deadline_s=0.2),
    )
    conversation_id = await harness.start(customer.id)

    outcome = await harness.say(conversation_id, "Help")

    turn = await turn_of(harness, outcome.turn_id)
    assert (outcome.status, turn.error) == (TurnStatus.BUDGET_EXHAUSTED, "turn deadline reached")
    assert outcome.escalation_number is not None


class StuckTools:
    """A tool server that never answers."""

    manifest = Manifest(
        (
            ToolDef(
                "get_order",
                "Get an order.",
                {"type": "object", "properties": {}, "additionalProperties": False},
                read_only=True,
                status_label="Checking your order…",
            ),
        )
    )

    @asynccontextmanager
    async def connect(self, scope: CustomerScope) -> AsyncIterator[ToolSession]:
        yield self

    async def list_tools(self) -> Manifest:
        return self.manifest

    async def call(self, name: str, arguments: dict[str, Any]) -> ToolAnswer:
        await asyncio.sleep(5)
        raise AssertionError("unreachable")


async def test_interrupted_tool_calls_still_get_results(
    make_harness: MakeHarness, factory: Factory
) -> None:
    customer = await factory.customer()
    harness = make_harness(
        [Reply(calls=(Call("get_order", {}),)), Reply(text="Back again.")],
        tools=StuckTools(),
        limits=AgentLimits(turn_deadline_s=0.3),
    )
    conversation_id = await harness.start(customer.id)

    first = await harness.say(conversation_id, "Where is it?")
    second = await harness.say(conversation_id, "Hello?")

    assert first.status is TurnStatus.BUDGET_EXHAUSTED
    messages = list(
        await harness.session.scalars(
            select(Message).where(Message.conversation_id == conversation_id).order_by(Message.seq)
        )
    )
    results = next(m for m in messages if m.kind is K.TOOL_RESULTS)
    assert results.content["results"][0]["content"].startswith("[INTERRUPTED]")
    # The conversation is escalated now, so the next message is acknowledged without a model call.
    assert second.replies[0].startswith("Thanks, I've added that to your case")
    assert len(harness.provider.requests) == 1
    harness.check_requests()


async def test_a_refusal_ends_the_turn_without_touching_the_transcript(
    make_harness: MakeHarness, factory: Factory
) -> None:
    customer = await factory.customer()
    harness = make_harness(
        [
            Reply(text="Partial", stop=Stop.REFUSAL, refusal_category="cyber"),
            Reply(text="Sure, what's your order number?"),
        ]
    )
    conversation_id = await harness.start(customer.id)

    refused = await harness.say(conversation_id, "Something odd")
    after = await harness.say(conversation_id, "Never mind, where's my order?")

    assert refused.status is TurnStatus.REFUSED
    assert refused.replies == [
        "I'm sorry, but I can't help with that. Is there something about your orders I can "
        "help with?"
    ]
    assert after.status is TurnStatus.COMPLETED
    # The decline the customer saw is in the model's transcript too, so the next message
    # doesn't follow the system note directly.
    assert await harness.kinds(conversation_id) == [
        K.CUSTOMER,
        K.SYSTEM_NOTE,
        K.ASSISTANT,
        K.CUSTOMER,
        K.ASSISTANT,
    ]
    second = harness.provider.requests[1].messages
    assert second[2] == {
        "role": "assistant",
        "content": [{"type": "text", "text": refused.replies[0]}],
    }
    call = await harness.session.scalar(select(ModelCall).order_by(ModelCall.created_at))
    assert call is not None
    assert call.refusal_category == "cyber"
    conversation = await harness.session.get_one(Conversation, conversation_id)
    assert conversation.flags == [{"type": "refusal", "turn": 1}]
    harness.check_requests()


async def test_after_a_failed_turn_the_customer_sends_again(
    make_harness: MakeHarness, factory: Factory
) -> None:
    customer = await factory.customer()
    harness = make_harness([Fail("overloaded"), Reply(text="Here's your answer.")])
    conversation_id = await harness.start(customer.id)

    failed = await harness.say(conversation_id, "Status of my order?")
    again = await harness.say(conversation_id, "Status of my order?")

    assert failed.status is TurnStatus.FAILED
    assert failed.replies == [
        "Sorry, something went wrong on our side. Please try sending that again."
    ]
    assert harness.log.of("error") == [{"code": "turn_failed", "message": failed.replies[0]}]
    assert (await turn_of(harness, failed.turn_id)).error == "overloaded"
    conversation = await harness.session.get_one(Conversation, conversation_id)
    assert conversation.flags == [{"type": "failure", "turn": 1, "error": "overloaded"}]
    assert again.status is TurnStatus.COMPLETED
    assert (await turn_of(harness, again.turn_id)).trigger is TurnTrigger.CUSTOMER_MESSAGE
    harness.check_requests()


async def test_an_empty_model_response_fails_the_turn(
    make_harness: MakeHarness, factory: Factory
) -> None:
    customer = await factory.customer()

    class Empty(ScriptedProvider):
        async def complete(self, request: ModelRequest, on_event: EventSink) -> ModelResponse:
            response = await super().complete(request, on_event)
            return replace(response, raw=None)

    harness = make_harness(provider=Empty([Reply(text="")]))
    conversation_id = await harness.start(customer.id)

    outcome = await harness.say(conversation_id, "Hi")

    assert outcome.status is TurnStatus.FAILED
    assert "empty response" in ((await turn_of(harness, outcome.turn_id)).error or "")


async def test_max_tokens_is_retried_once_with_more_room(
    make_harness: MakeHarness, factory: Factory
) -> None:
    customer = await factory.customer()
    harness = make_harness([Reply(text="cut off", stop=Stop.MAX_TOKENS), Reply(text="Complete.")])
    conversation_id = await harness.start(customer.id)

    outcome = await harness.say(conversation_id, "Hi")

    assert (outcome.status, outcome.replies) == (TurnStatus.COMPLETED, ["Complete."])
    assert [r.request.max_tokens for r in harness.provider.requests] == [16_000, 32_000]
    assert await harness.kinds(conversation_id) == [K.CUSTOMER, K.SYSTEM_NOTE, K.ASSISTANT]
    calls = list(await harness.session.scalars(select(ModelCall).order_by(ModelCall.seq)))
    assert [(c.stop_reason, c.max_tokens) for c in calls] == [
        ("max_tokens", 16_000),
        ("end_turn", 32_000),
    ]


async def test_truncated_twice_fails(make_harness: MakeHarness, factory: Factory) -> None:
    customer = await factory.customer()
    harness = make_harness(
        [Reply(text="cut", stop=Stop.MAX_TOKENS), Reply(text="cut again", stop=Stop.MAX_TOKENS)]
    )
    conversation_id = await harness.start(customer.id)

    outcome = await harness.say(conversation_id, "Hi")

    assert outcome.status is TurnStatus.FAILED
    assert (await turn_of(harness, outcome.turn_id)).error == "output truncated twice"
    assert await harness.kinds(conversation_id) == [K.CUSTOMER, K.SYSTEM_NOTE, K.ASSISTANT]
