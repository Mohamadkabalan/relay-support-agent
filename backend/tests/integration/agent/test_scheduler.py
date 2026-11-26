"""The turn scheduler (ADR-0007): no lost wake-ups, recovery of orphaned turns, the sweep."""

import asyncio
import json
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection
from tests.integration.agent.conftest import MakeHarness
from tests.support.agent import EventLog, Harness, assert_valid_transcript
from tests.support.factories import Factory, rollback_sessionmaker

from relay.agent.loop import FAILURE_TEXT, RECOVERED_ERROR
from relay.agent.providers.base import EventSink, ModelRequest, ModelResponse
from relay.agent.providers.scripted import Call, Reply, ScriptedProvider, ScriptedRequest
from relay.agent.scheduler import Scheduler
from relay.agent.transcript import Item
from relay.clock import FrozenClock
from relay.db.models import Approval, Conversation, InboxItem, Turn
from relay.domain.enums import ApprovalStatus, ConversationStatus, TurnStatus, TurnTrigger


class Hooked:
    """A scripted provider that runs `before` (once per listed request number) first."""

    def __init__(self, inner: ScriptedProvider, before: dict[int, Any]) -> None:
        self.inner = inner
        self.before = before
        self.name, self.model, self.effort = inner.name, inner.model, inner.effort

    def render(self, item: Item) -> str:
        return self.inner.render(item)

    async def complete(self, request: ModelRequest, on_event: EventSink) -> ModelResponse:
        hook = self.before.pop(len(self.inner.requests) + 1, None)
        if hook is not None:
            await hook()
        return await self.inner.complete(request, on_event)


def scheduler_for(harness: Harness, connection: AsyncConnection) -> Scheduler:
    return Scheduler(harness.agent, rollback_sessionmaker(connection), publish=EventLog())


async def test_a_message_sent_during_a_turn_gets_its_own_turn(
    make_harness: MakeHarness, factory: Factory, connection: AsyncConnection
) -> None:
    customer = await factory.customer()
    scripted = ScriptedProvider([Reply(text="First answer."), Reply(text="Second answer.")])
    harness = make_harness(provider=Hooked(scripted, {}))
    scheduler = scheduler_for(harness, connection)
    conversation_id = await harness.start(customer.id)

    async def meanwhile() -> None:
        # The customer types again mid-turn: this kick can't take the lock and gives up at once.
        await harness.agent.store.enqueue_message(conversation_id, "And one more thing")
        await scheduler.kick(conversation_id)

    harness.agent.provider = Hooked(scripted, {1: meanwhile})
    await harness.agent.store.enqueue_message(conversation_id, "Hello")
    await scheduler.kick(conversation_id)
    await scheduler.idle()

    turns = list(
        await harness.session.scalars(
            select(Turn).where(Turn.conversation_id == conversation_id).order_by(Turn.seq)
        )
    )
    assert [(t.seq, t.status) for t in turns] == [
        (1, TurnStatus.COMPLETED),
        (2, TurnStatus.COMPLETED),
    ]
    second_request = scripted.requests[1].messages
    assert second_request[-1] == {
        "role": "user",
        "content": [{"type": "text", "text": "And one more thing"}],
    }
    assert not await harness.agent.store.has_waiting(conversation_id)


async def test_an_orphaned_turn_is_finished_and_the_conversation_goes_on(
    make_harness: MakeHarness, factory: Factory, connection: AsyncConnection
) -> None:
    customer = await factory.customer()
    order = await factory.order(customer)
    stuck = asyncio.Event()

    async def hang() -> None:
        stuck.set()
        await asyncio.Event().wait()  # never answers

    scripted = ScriptedProvider(
        [Reply(calls=(Call("get_order", {"order_number": order.number}),)), Reply(text="Later.")]
    )
    hooked = Hooked(scripted, {2: hang})
    harness = make_harness(provider=hooked)
    scheduler = scheduler_for(harness, connection)
    conversation_id = await harness.start(customer.id)

    # The process "stops" while the second model call is in flight.
    running = asyncio.create_task(harness.agent.send(conversation_id, "Where is my order?"))
    await stuck.wait()
    running.cancel()
    await asyncio.gather(running, return_exceptions=True)
    orphan = await harness.session.scalar(
        select(Turn).where(Turn.conversation_id == conversation_id)
    )
    assert orphan is not None
    assert orphan.status is TurnStatus.RUNNING

    assert await scheduler.recover_orphans() == [conversation_id]
    await harness.session.refresh(orphan)
    assert (orphan.status, orphan.error) == (TurnStatus.FAILED, RECOVERED_ERROR)
    messages = await harness.agent.store.messages(conversation_id)
    assert messages[-1].content["text"] == FAILURE_TEXT

    harness.agent.provider = scripted  # the customer tries again, as the apology asks
    outcome = await harness.say(conversation_id, "Where is my order?")
    assert outcome.replies == ["Later."]
    assert_valid_transcript(scripted.requests[-1].messages)
    assert await scheduler.recover_orphans() == []


async def test_a_turn_orphaned_before_it_started_gives_its_inputs_back(
    make_harness: MakeHarness, factory: Factory, connection: AsyncConnection
) -> None:
    customer = await factory.customer()
    harness = make_harness([Reply(text="Hello, how can I help?")])
    scheduler = scheduler_for(harness, connection)
    conversation_id = await harness.start(customer.id)
    store = harness.agent.store
    await store.enqueue_message(conversation_id, "Hi there")
    claimed = await store.start_turn(conversation_id)  # claimed, then the process stopped
    assert claimed is not None
    assert not await store.has_waiting(conversation_id)

    await scheduler.recover_orphans()
    await scheduler.idle()

    turns = list(
        await harness.session.scalars(
            select(Turn).where(Turn.conversation_id == conversation_id).order_by(Turn.seq)
        )
    )
    assert [t.status for t in turns] == [TurnStatus.FAILED, TurnStatus.COMPLETED]
    texts = [m.content.get("text") for m in await store.messages(conversation_id)]
    assert texts.count("Hi there") == 1
    assert texts[-1] == "Hello, how can I help?"


async def test_an_expired_approval_resumes_the_conversation(
    make_harness: MakeHarness, factory: Factory, connection: AsyncConnection, clock: FrozenClock
) -> None:
    customer = await factory.customer()
    order = await factory.order(customer, lines=(("AUD-ANC700", 1),), delivered_days_ago=3)
    refund = {
        "order_number": order.number,
        "reason": "damaged_or_defective",
        "lines": [{"line": 1, "quantity": 1}],
    }

    def commit(sent: ScriptedRequest) -> Reply:
        token = sent.last_tool_result["confirmation_token"]
        return Reply(calls=(Call("issue_refund", {**refund, "confirmation_token": token}),))

    def told(sent: ScriptedRequest) -> Reply:
        assert "expired" in sent.messages[-1]["content"]
        return Reply(text="Sorry, that request expired; our team will follow up.")

    harness = make_harness(
        [
            Reply(calls=(Call("issue_refund", refund),)),
            Reply(text="$349.99 needs a review. Go ahead?"),
            commit,
            Reply(text="Submitted."),
            told,
        ]
    )
    scheduler = scheduler_for(harness, connection)
    conversation_id = await harness.start(customer.id)
    await harness.say(conversation_id, "My headphones arrived cracked.")
    await harness.say(conversation_id, "Yes")

    clock.advance(timedelta(hours=25))
    numbers = await scheduler.expire_approvals()
    await scheduler.idle()

    assert len(numbers) == 1
    approval = await harness.session.scalar(
        select(Approval).where(Approval.approval_number == numbers[0])
    )
    assert approval is not None
    assert approval.status is ApprovalStatus.EXPIRED
    last = await harness.agent.store.last_turn(conversation_id)
    assert last is not None
    assert (last.trigger, last.status) == (TurnTrigger.APPROVAL_RESOLVED, TurnStatus.COMPLETED)
    published = scheduler.publish
    assert isinstance(published, EventLog)
    assert [e.type for e in published.events][:1] == ["approval.resolved"]


async def test_idle_conversations_close_unless_something_is_pending(
    make_harness: MakeHarness, factory: Factory, connection: AsyncConnection, clock: FrozenClock
) -> None:
    customer = await factory.customer()
    harness = make_harness([Reply(text="Hi!")])
    scheduler = scheduler_for(harness, connection)
    idle = await harness.start(customer.id)
    await harness.say(idle, "Hello")
    waiting = await harness.start(customer.id)
    await harness.agent.store.enqueue_message(waiting, "Are you there?")

    clock.advance(timedelta(minutes=29))
    assert await scheduler.close_idle() == []
    clock.advance(timedelta(minutes=2))
    assert await scheduler.close_idle() == [idle]

    rows = {
        row.id: row.status
        for row in await harness.session.scalars(
            select(Conversation).where(Conversation.id.in_([idle, waiting]))
        )
    }
    assert rows == {idle: ConversationStatus.CLOSED, waiting: ConversationStatus.ACTIVE}
    unclaimed = await harness.session.scalar(
        select(InboxItem).where(InboxItem.conversation_id == waiting)
    )
    assert unclaimed is not None
    assert json.dumps(unclaimed.payload) == '{"text": "Are you there?"}'
