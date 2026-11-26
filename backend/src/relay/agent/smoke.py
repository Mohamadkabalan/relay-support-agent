"""`relay smoke` (`make smoke`): one live conversation with Claude, checked end to end.

It needs ANTHROPIC_API_KEY and costs a few cents. On a throwaway database it creates a customer
whose $59.99 earbuds arrived broken 3 days ago, then plays the customer until a refund is issued
(at most `MAX_CUSTOMER_TURNS` messages, reacting to what the database shows):

1. a complaint, which should lead to a refund preview;
2. a confirmation, which should commit it (under the $100 auto-approve limit).

The run passes when a refund was issued, no turn failed, the second model call read from the
prompt cache (`cache_read_input_tokens > 0`), and every request was sent with
`prefix_mismatch_behavior: "error"`, so any history edit would have failed with a 400.
"""

import secrets
import uuid
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from relay.agent.chat import TerminalView
from relay.agent.factory import build_agent, build_provider, in_process_tools
from relay.agent.providers.anthropic import AnthropicProvider
from relay.clock import SystemClock, local_date
from relay.config import Settings
from relay.db import migrate
from relay.db.admin import create_database, drop_database
from relay.db.models import ActionPreview, ModelCall, Refund, Turn
from relay.db.session import create_engine, make_sessionmaker
from relay.domain.address import Address
from relay.domain.enums import OrderStatus, ShipmentStatus, TurnStatus
from relay.domain.policy_config import default_policy
from relay.kb.embeddings import HashingEmbedder
from relay.seed import builders
from relay.seed.builders import Ids, OrderPlan, TrackingPlan, at_local

MAX_CUSTOMER_TURNS = 5
SKU = "AUD-BUDSL"  # Aurora Buds Lite, $59.99

COMPLAINT = (
    "Hi, the Aurora Buds Lite from my order {order} arrived three days ago and the left earbud "
    "doesn't work at all. Can I get my money back for them?"
)
CONFIRM = "Yes, please go ahead with that refund."
NUDGE = "It's order {order}, the earbuds are defective. Please refund them."


@dataclass
class SmokeResult:
    passed: bool
    checks: list[tuple[str, bool]]


async def run_smoke(
    settings: Settings, view: TerminalView, provider: AnthropicProvider | None = None
) -> SmokeResult:
    """`provider` is for testing this harness against a fake API; by default, the real one."""
    if provider is None:
        built = build_provider(settings, prefix_mismatch="error")
        if not isinstance(built, AnthropicProvider):
            raise RuntimeError("make smoke checks the Claude provider; unset RELAY_PROVIDER.")
        provider = built
    server_url = str(settings.database_url)
    name = f"relay_smoke_{secrets.token_hex(4)}"
    url = await create_database(server_url, name)
    engine = create_engine(url, pool_size=2)
    try:
        await migrate.upgrade(engine)
        customer_id, order_number = await _fixture(engine)
        return await _converse(settings, engine, provider, customer_id, order_number, view)
    finally:
        await engine.dispose()
        await drop_database(server_url, name)


async def _converse(
    settings: Settings,
    engine: AsyncEngine,
    provider: AnthropicProvider,
    customer_id: uuid.UUID,
    order_number: str,
    view: TerminalView,
) -> SmokeResult:
    sessionmaker = make_sessionmaker(engine)
    clock, policy = SystemClock(), default_policy()
    tools = in_process_tools(
        sessionmaker, HashingEmbedder(), clock=clock, policy=policy, store_name=settings.store_name
    )
    agent = build_agent(
        settings, sessionmaker, provider=provider, tools=tools, listener=view.on_event
    )
    conversation_id = await agent.start_conversation(customer_id)
    view.line(f"Smoke test: conversation {conversation_id}, {provider.model}")

    message = COMPLAINT.format(order=order_number)
    for _ in range(MAX_CUSTOMER_TURNS):
        view.line(f"you> {message}")
        outcome = await agent.send(conversation_id, message)
        if outcome is None or outcome.status is not TurnStatus.COMPLETED:
            break  # no point going on; the checks below report it
        async with sessionmaker() as session:
            if await _count(session, Refund) > 0:
                break
            previews = await _count(session, ActionPreview)
        message = CONFIRM if previews else NUDGE.format(order=order_number)

    async with sessionmaker() as session:
        refunds = await _count(session, Refund)
        statuses = list(await session.scalars(select(Turn.status).order_by(Turn.seq)))
        calls = list(
            await session.scalars(select(ModelCall).join(Turn).order_by(Turn.seq, ModelCall.seq))
        )
    _report(calls, view)
    checks = [
        ("a refund was issued", refunds == 1),
        ("no turn failed", TurnStatus.FAILED not in statuses),
        ("every turn completed", all(status is TurnStatus.COMPLETED for status in statuses)),
        ("the second model call read the cache", len(calls) > 1 and calls[1].cache_read_tokens > 0),
    ]
    for label, ok in checks:
        view.line(f"{'PASS' if ok else 'FAIL'}  {label}")
    return SmokeResult(all(ok for _, ok in checks), checks)


def _report(calls: list[ModelCall], view: TerminalView) -> None:
    view.line("model calls (stop, input, cache read, cache write, output, cost, request id):")
    for call in calls:
        cost = "?" if call.cost_micros is None else f"${call.cost_micros / 1_000_000:.4f}"
        view.line(
            f"  {call.stop_reason:<9} {call.input_tokens:>7,} {call.cache_read_tokens:>7,} "
            f"{call.cache_write_tokens:>7,} {call.output_tokens:>6,} {cost:>9}  "
            f"{call.served_model} {call.request_id or ''}"
        )


async def _count(session: AsyncSession, model: type[Refund] | type[ActionPreview]) -> int:
    return int(await session.scalar(select(func.count()).select_from(model)) or 0)


async def _fixture(engine: AsyncEngine) -> tuple[uuid.UUID, str]:
    """A customer whose earbuds were delivered 3 days ago."""
    policy = default_policy()
    now = SystemClock().now()
    today = local_date(now, policy.tz)
    delivered = at_local(today - timedelta(days=3), 14, 0, policy.tz)
    placed = delivered - timedelta(days=4)
    address = Address(
        recipient_name="Sky Tester",
        line1="1 Smoke Street",
        city="Springfield",
        state="IL",
        postal_code="62701",
    )
    ids = Ids()
    async with AsyncSession(engine, expire_on_commit=False) as session, session.begin():
        session.add_all(builders.products())
        customer = builders.customer(
            ids,
            email="sky.tester@example.com",
            full_name="Sky Tester",
            created_at=now - timedelta(days=200),
            address=address,
        )
        session.add(customer)
        number = "PV-900001"
        session.add(
            builders.order(
                ids,
                customer,
                OrderPlan(
                    number=number,
                    placed_at=placed,
                    status=OrderStatus.DELIVERED,
                    lines=[(SKU, 1)],
                    shipping_address=address,
                    shipping_cents=599,
                    promised_delivery_on=local_date(placed, policy.tz) + timedelta(days=6),
                    tracking=TrackingPlan(
                        carrier="UPS",
                        tracking_number="1Z900001SMOKE",
                        status=ShipmentStatus.DELIVERED,
                        shipped_at=placed + timedelta(hours=20),
                        delivered_at=delivered,
                    ),
                ),
            )
        )
    return customer.id, number
