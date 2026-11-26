"""Turns that do their job: answering, recovering from a tool error, confirm-then-commit across
turns, and resuming after an operator's decision (PLAN.md §5.1, §5.9)."""

from sqlalchemy import select
from tests.integration.agent.conftest import MakeHarness
from tests.support.factories import Factory

from relay.agent.providers.scripted import Call, Reply, ScriptedRequest
from relay.agent.transcript import EVENT_LINE
from relay.db.models import Conversation, ModelCall, Refund, Turn
from relay.domain import approvals
from relay.domain.enums import (
    ApprovalStatus,
    ConversationStatus,
    MessageKind,
    ToolCallOutcome,
    TurnStatus,
    TurnTrigger,
)
from relay.domain.scope import Deps

K = MessageKind


async def test_a_simple_turn(make_harness: MakeHarness, factory: Factory) -> None:
    customer = await factory.customer("Dana Whitfield")
    harness = make_harness([Reply(text="Hi Dana! How can I help with your orders?")])
    conversation_id = await harness.start(customer.id)

    outcome = await harness.say(conversation_id, "Hello")

    assert outcome.status is TurnStatus.COMPLETED
    assert outcome.replies == ["Hi Dana! How can I help with your orders?"]
    sent = harness.provider.requests[0]
    assert sent.messages == [
        {"role": "user", "content": [{"type": "text", "text": "Hello"}]},
        {
            "role": "system",
            "content": "Current date and time: Tuesday 2026-09-15, 15:00 (America/New_York). "
            "Signed-in customer: Dana Whitfield.",
        },
    ]
    conversation = await harness.session.get_one(Conversation, conversation_id)
    assert sent.request.system == conversation.system_prompt
    assert [tool.name for tool in sent.request.tools] == sorted(
        tool.name for tool in sent.request.tools
    )
    assert len(sent.request.tools) == 9
    assert sent.request.user_key
    assert str(customer.id) not in sent.request.user_key
    assert await harness.kinds(conversation_id) == [K.CUSTOMER, K.SYSTEM_NOTE, K.ASSISTANT]

    turn = await harness.session.scalar(select(Turn).where(Turn.conversation_id == conversation_id))
    assert turn is not None
    assert (turn.status, turn.trigger, turn.steps, turn.seq) == (
        TurnStatus.COMPLETED,
        TurnTrigger.CUSTOMER_MESSAGE,
        1,
        1,
    )
    assert (turn.input_tokens, turn.output_tokens) == (1000, 100)
    await harness.session.refresh(conversation)
    assert conversation.processed_tokens == 1100
    assert harness.log.types() == ["turn.started", "message.completed", "turn.completed"]


async def test_a_tool_error_is_data_the_model_recovers_from(
    make_harness: MakeHarness, factory: Factory
) -> None:
    customer = await factory.customer()
    order = await factory.order(customer)

    def after_error(sent: ScriptedRequest) -> Reply:
        result = sent.messages[-1]["content"][0]
        assert result["is_error"] is True
        assert result["content"].startswith("[ORDER_NOT_FOUND]")
        return Reply(calls=(Call("list_orders", {}),))

    harness = make_harness(
        [
            Reply(calls=(Call("get_order", {"order_number": "PV-000001"}),)),
            after_error,
            Reply(calls=(Call("list_orders", {}),)),  # the same call again: not run twice
            Reply(text=f"I found your order {order.number}."),
        ]
    )
    conversation_id = await harness.start(customer.id)

    outcome = await harness.say(conversation_id, "Where's my order?")

    assert outcome.status is TurnStatus.COMPLETED
    calls = await harness.tool_calls(conversation_id)
    assert [(c.name, c.outcome, c.error_code) for c in calls] == [
        ("get_order", ToolCallOutcome.ERROR, "ORDER_NOT_FOUND"),
        ("list_orders", ToolCallOutcome.OK, None),
        ("list_orders", ToolCallOutcome.DUPLICATE, None),
    ]
    assert calls[2].result.startswith("[DUPLICATE]")
    assert harness.log.of("status")[0] == {"label": "Checking your order…"}
    harness.check_requests()


async def test_confirm_then_commit_across_turns(
    make_harness: MakeHarness, factory: Factory
) -> None:
    customer = await factory.customer()
    order = await factory.order(customer, lines=(("AUD-BUDSL", 1),), delivered_days_ago=3)
    refund = {
        "order_number": order.number,
        "reason": "damaged_or_defective",
        "lines": [{"line": 1, "quantity": 1}],
    }

    def confirm_too_early(sent: ScriptedRequest) -> Reply:
        token = sent.last_tool_result["confirmation_token"]
        return Reply(calls=(Call("issue_refund", {**refund, "confirmation_token": token}),))

    def ask(sent: ScriptedRequest) -> Reply:
        assert "[CONFIRMATION_TOO_EARLY]" in sent.messages[-1]["content"][0]["content"]
        return Reply(text="I can refund $59.99 to your Visa ending 4242. Shall I go ahead?")

    def commit(sent: ScriptedRequest) -> Reply:
        token = sent.last_tool_result["confirmation_token"]  # the preview from the last turn
        return Reply(calls=(Call("issue_refund", {**refund, "confirmation_token": token}),))

    def done(sent: ScriptedRequest) -> Reply:
        result = sent.last_tool_result
        assert result["status"] == "completed"
        return Reply(text=f"Done: refund {result['refund_number']} is on its way.")

    harness = make_harness(
        [Reply(calls=(Call("issue_refund", refund),)), confirm_too_early, ask, commit, done]
    )
    conversation_id = await harness.start(customer.id)

    first = await harness.say(conversation_id, "My earbuds arrived broken, refund please.")
    assert await harness.session.scalar(select(Refund).where(Refund.order_id == order.id)) is None
    second = await harness.say(conversation_id, "Yes please")

    assert (first.status, second.status) == (TurnStatus.COMPLETED, TurnStatus.COMPLETED)
    issued = await harness.session.scalar(select(Refund).where(Refund.order_id == order.id))
    assert issued is not None
    assert issued.amount_cents == 5999
    assert issued.conversation_id == conversation_id
    outcomes = [(c.outcome, c.error_code) for c in await harness.tool_calls(conversation_id)]
    assert outcomes == [
        (ToolCallOutcome.OK, None),  # preview
        (ToolCallOutcome.ERROR, "CONFIRMATION_TOO_EARLY"),  # same turn
        (ToolCallOutcome.OK, None),  # the next turn
    ]
    harness.check_requests()


async def test_resuming_after_an_operator_approves(
    make_harness: MakeHarness, factory: Factory, deps: Deps
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

    def resumed(sent: ScriptedRequest) -> Reply:
        assert sent.messages[-2] == {
            "role": "user",
            "content": [{"type": "text", "text": EVENT_LINE}],
        }
        note = sent.messages[-1]
        assert note["role"] == "system"
        assert "approved it and it has been carried out" in note["content"]
        assert "RF-" in note["content"]
        return Reply(text="Good news: your $349.99 refund was approved and issued.")

    harness = make_harness(
        [
            Reply(calls=(Call("issue_refund", refund),)),
            Reply(text="That's $349.99 to your Visa. A team member must approve it. Go ahead?"),
            commit,
            Reply(text="Submitted. You'll see the decision here."),
            resumed,
        ]
    )
    conversation_id = await harness.start(customer.id)
    await harness.say(conversation_id, "My headphones arrived cracked.")
    await harness.say(conversation_id, "Yes")
    pending = harness.log.of("approval.pending")
    assert len(pending) == 1
    number = pending[0]["approval_number"]

    operator = await factory.operator()
    view = await approvals.resolve(deps, factory.operator_scope(operator), number, approve=True)
    assert view.status is ApprovalStatus.EXECUTED
    await harness.agent.store.enqueue_approval(conversation_id, view)
    outcome = await harness.agent.run_turn(conversation_id)

    assert outcome is not None
    assert outcome.status is TurnStatus.COMPLETED
    assert outcome.replies == ["Good news: your $349.99 refund was approved and issued."]
    turn = await harness.session.get_one(Turn, outcome.turn_id)
    assert turn.trigger is TurnTrigger.APPROVAL_RESOLVED
    stored = await harness.agent.store.events(conversation_id)
    resolved = [event.payload for event in stored if event.type == "approval.resolved"]
    assert resolved == [{"approval_number": number, "decision": "executed"}]
    assert (await harness.kinds(conversation_id))[-3:] == [K.EVENT, K.SYSTEM_NOTE, K.ASSISTANT]
    conversation = await harness.session.get_one(Conversation, conversation_id)
    assert conversation.status is ConversationStatus.ACTIVE
    harness.check_requests()


async def test_an_empty_inbox_runs_no_turn(make_harness: MakeHarness, factory: Factory) -> None:
    customer = await factory.customer()
    harness = make_harness()
    conversation_id = await harness.start(customer.id)

    assert await harness.agent.run_turn(conversation_id) is None
    assert await harness.session.scalar(select(ModelCall)) is None
