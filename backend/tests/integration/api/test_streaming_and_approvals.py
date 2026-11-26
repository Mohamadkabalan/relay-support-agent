"""SSE resume, the approval flow end to end, and the spend cap (Phase 5 acceptance criteria)."""

import httpx2
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from tests.support.api import customer_by_visitor_email, open_stream, running_api, turn_completed
from tests.support.factories import make_engine

from relay.agent.loop import PAUSED_TEXT
from relay.agent.providers.scripted import Call, Reply, ScriptedRequest, Step
from relay.agent.spend import RedisSpend
from relay.clock import FrozenClock
from relay.db.models import AuditLog, Order, Refund, Turn
from relay.domain.enums import ActorType, TurnStatus


async def sign_in(client: httpx2.AsyncClient, persona: str = "dana") -> None:
    assert (await client.post("/api/auth/demo", json={"persona": persona})).status_code == 200


async def test_a_reconnect_replays_the_events_it_missed(
    api_database: str, clock: FrozenClock
) -> None:
    replies = [Reply(text="Hello! How can I help?"), Reply(text="Happy to check that order.")]
    async with running_api(api_database, replies, clock=clock) as api, api.client() as client:
        await sign_in(client)
        conversation = (await client.post("/api/conversations")).json()["id"]
        path = f"/api/conversations/{conversation}"

        async with open_stream(client, f"{path}/events") as stream:
            assert (await client.post(f"{path}/messages", json={"text": "Hi"})).status_code == 202
            first = await stream.until(turn_completed)
        types = [e.event for e in first]
        assert types[0] == "turn.started"
        assert "message.delta" in types  # live text arrives before the completed message
        assert types[-2:] == ["message.completed", "turn.completed"]
        last_seen = max(e.id for e in first if e.id is not None)
        assert all(e.id is None for e in first if e.event == "message.delta")

        # Disconnected: a second turn runs and its events are stored.
        await client.post(f"{path}/messages", json={"text": "Where is my order?"})
        await api.idle()

        async with open_stream(client, f"{path}/events", last_event_id=last_seen) as stream:
            missed = await stream.until(turn_completed)
        assert [e.event for e in missed] == ["turn.started", "message.completed", "turn.completed"]
        assert all(e.id is not None and e.id > last_seen for e in missed)
        assert missed[1].data["text"] == "Happy to check that order."


async def test_an_approved_refund_reaches_the_customer_over_sse(
    api_database: str, clock: FrozenClock
) -> None:
    """Dana's $349.99 refund: preview, confirm, pending, operator approves, the agent resumes."""
    order: dict[str, str] = {}

    def refund() -> dict[str, object]:
        lines = [{"line": 1, "quantity": 1}]
        return {"order_number": order["number"], "reason": "damaged_or_defective", "lines": lines}

    def preview(_sent: ScriptedRequest) -> Reply:
        return Reply(calls=(Call("issue_refund", refund()),))

    def commit(sent: ScriptedRequest) -> Reply:
        token = sent.last_tool_result["confirmation_token"]
        return Reply(calls=(Call("issue_refund", {**refund(), "confirmation_token": token}),))

    def resumed(sent: ScriptedRequest) -> Reply:
        assert sent.messages[-1]["role"] == "system"
        assert "approved it and it has been carried out" in sent.messages[-1]["content"]
        return Reply(text="Good news: your $349.99 refund was approved.")

    steps: list[Step] = [
        preview,
        Reply(text="I can refund $349.99 to your Visa ending 4242. It needs a quick review. OK?"),
        commit,
        Reply(text="Submitted. You'll see the decision right here."),
        resumed,
    ]
    async with running_api(api_database, steps, clock=clock) as api, api.client() as client:
        await sign_in(client)
        dana = await customer_by_visitor_email(api_database, "dana.whitfield+")
        engine = make_engine(api_database)
        async with AsyncSession(engine) as db:
            numbers = await db.scalars(select(Order.number).where(Order.customer_id == dana.id))
            order["number"] = sorted(numbers)[0]  # the headphones order comes first
        conversation = (await client.post("/api/conversations")).json()["id"]
        path = f"/api/conversations/{conversation}"

        async with open_stream(client, f"{path}/events") as stream:
            await client.post(f"{path}/messages", json={"text": "My headphones arrived cracked."})
            await stream.until(turn_completed)
            await client.post(f"{path}/messages", json={"text": "Yes please."})
            second = await stream.until(turn_completed)
            pending = [e.data for e in second if e.event == "approval.pending"]
            assert len(pending) == 1
            number = pending[0]["approval_number"]

            # The same browser opens the demo console (the session now holds both roles).
            assert (await client.post("/api/auth/demo-operator")).status_code == 200
            queue = (await client.get("/api/console/approvals")).json()
            assert [row["approval_number"] for row in queue] == [number]
            assert queue[0]["amount_cents"] == 34999
            decided = await client.post(
                f"/api/console/approvals/{number}/decision",
                json={"decision": "approve", "note": "Photo shows the crack."},
            )
            assert decided.status_code == 200, decided.text
            assert decided.json()["status"] == "executed"

            resumed_events = await stream.until(turn_completed)
        kinds = [e.event for e in resumed_events]
        assert kinds[0] == "approval.resolved"
        assert resumed_events[0].data == {"approval_number": number, "decision": "executed"}
        completed = [e.data["text"] for e in resumed_events if e.event == "message.completed"]
        assert completed == ["Good news: your $349.99 refund was approved."]

        async with AsyncSession(engine) as db:
            refunds = list(
                await db.scalars(select(Refund).join(Order).where(Order.number == order["number"]))
            )
            assert [r.amount_cents for r in refunds] == [34999]
            assert refunds[0].approval_id is not None
            audit = list(
                await db.scalars(
                    select(AuditLog).where(AuditLog.approval_id == refunds[0].approval_id)
                )
            )
            by_operator = [row for row in audit if row.actor_type is ActorType.OPERATOR]
            assert any(row.entity == "refund" for row in by_operator), [r.action for r in audit]
            assert all(row.trace_id is None or len(row.trace_id) == 32 for row in audit)
        await engine.dispose()

        replay = (await client.get(f"/api/console/conversations/{conversation}")).json()
        assert [t["trigger"] for t in replay["turns"]] == [
            "customer_message",
            "customer_message",
            "approval_resolved",
        ]
        chat = (await client.get(path)).json()
        assert chat["messages"][-1] == {
            **chat["messages"][-1],
            "author": "assistant",
            "text": "Good news: your $349.99 refund was approved.",
        }
        assert chat["pending_approvals"] == []


async def test_no_model_call_starts_once_the_cap_would_be_passed(
    api_database: str, clock: FrozenClock
) -> None:
    async with (
        running_api(
            api_database, [Reply(text="never sent")], clock=clock, daily_spend_cap_usd=0.05
        ) as api,
        api.client() as client,
    ):
        await sign_in(client)
        conversation = (await client.post("/api/conversations")).json()["id"]
        path = f"/api/conversations/{conversation}"
        spend = api.services.spend
        assert isinstance(spend, RedisSpend)

        async with open_stream(client, f"{path}/events") as stream:
            await client.post(f"{path}/messages", json={"text": "Hi"})
            events = await stream.until(turn_completed)

        assert api.provider.requests == []  # the worst case of one call is over $0.05
        assert events[-1].data["status"] == "paused"
        assert [e.data["code"] for e in events if e.event == "error"] == ["paused"]
        messages = (await client.get(path)).json()["messages"]
        assert messages[-1]["text"] == PAUSED_TEXT
        engine = make_engine(api_database)
        async with AsyncSession(engine) as db:
            statuses = list(
                await db.scalars(select(Turn.status).where(Turn.conversation_id == conversation))
            )
        await engine.dispose()
        assert statuses == [TurnStatus.PAUSED]
        assert await spend.spent_today() == 0
