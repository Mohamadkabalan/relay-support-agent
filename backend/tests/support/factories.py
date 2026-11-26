"""Test support: temporary databases and a factory for customers and orders in known states.

All tests run on the eval clock: 2026-09-15 15:00 in New York.
"""

import itertools
import os
import secrets
import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from relay.clock import FrozenClock, local_date
from relay.db import migrate
from relay.db.admin import create_database, drop_database
from relay.db.models import AuditLog, Customer, Operator, Order
from relay.db.session import database_name
from relay.domain.address import Address
from relay.domain.enums import OrderStatus, ShipmentStatus
from relay.domain.policy_config import PolicyConfig
from relay.domain.scope import CustomerScope, OperatorScope
from relay.seed import builders
from relay.seed.builders import Ids, OrderPlan, TrackingPlan, at_local

STORE_TZ = ZoneInfo("America/New_York")
NOW = datetime(2026, 9, 15, 15, 0, tzinfo=STORE_TZ)


def server_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.fail(
            "Integration tests need DATABASE_URL. Run them with `make test`, which starts "
            "Postgres and sets it."
        )
    return url


def rollback_sessionmaker(connection: AsyncConnection) -> async_sessionmaker[AsyncSession]:
    """Sessions on a test's connection. Their commits only release savepoints, so the test's
    rollback still undoes everything, and each session sees what the others flushed."""
    return async_sessionmaker(
        bind=connection, join_transaction_mode="create_savepoint", expire_on_commit=False
    )


def make_engine(url: str) -> AsyncEngine:
    return create_async_engine(
        url, poolclass=NullPool, connect_args={"server_settings": {"timezone": "UTC"}}
    )


async def create_test_database(*, migrated: bool = True, catalog: bool = False) -> str:
    """A brand-new database on the test server. The caller drops it with `drop_test_database`."""
    name = f"relay_test_{secrets.token_hex(5)}"
    url = await create_database(server_url(), name)
    engine = make_engine(url)
    try:
        if migrated:
            await migrate.upgrade(engine)
        if catalog:
            async with AsyncSession(engine) as session, session.begin():
                session.add_all(builders.products())
    finally:
        await engine.dispose()
    return url


async def drop_test_database(url: str) -> None:
    await drop_database(server_url(), database_name(url))


@asynccontextmanager
async def temporary_database(*, migrated: bool = True, catalog: bool = False) -> AsyncIterator[str]:
    """A brand-new database, dropped on exit. For tests that need real commits."""
    url = await create_test_database(migrated=migrated, catalog=catalog)
    try:
        yield url
    finally:
        await drop_test_database(url)


_numbers = itertools.count(900001 + secrets.randbelow(50000))


class Factory:
    """Builds customers and orders in specific states, relative to the frozen clock."""

    def __init__(self, session: AsyncSession, clock: FrozenClock, policy: PolicyConfig) -> None:
        self.session = session
        self.clock = clock
        self.policy = policy
        self.ids = Ids()

    @property
    def now(self) -> datetime:
        return self.clock.now()

    def address(self, name: str = "Test Customer", country: str = "US") -> Address:
        return Address(
            recipient_name=name,
            line1="12 Test Street",
            city="Springfield",
            state="IL" if country == "US" else "Ontario",
            postal_code="62701" if country == "US" else "K1A 0B1",
            country=country,
        )

    async def customer(self, name: str = "Test Customer") -> Customer:
        token = secrets.token_hex(4)
        row = builders.customer(
            self.ids,
            email=f"test.{token}@example.com",
            full_name=name,
            created_at=self.now - timedelta(days=400),
            address=self.address(name),
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def operator(self) -> Operator:
        row = Operator(email=f"op.{secrets.token_hex(4)}@example.com", display_name="Test Operator")
        self.session.add(row)
        await self.session.flush()
        return row

    def scope(
        self, owner: Customer, *, conversation: uuid.UUID | None = None, turn: int | None = 1
    ) -> CustomerScope:
        return CustomerScope(
            customer_id=owner.id,
            conversation_id=conversation if conversation is not None else uuid.uuid7(),
            turn_seq=turn,
        )

    def operator_scope(self, operator: Operator) -> OperatorScope:
        return OperatorScope(operator_id=operator.id)

    def turns(self, owner: Customer) -> tuple[CustomerScope, CustomerScope]:
        """Two consecutive turns of one conversation: preview in the first, commit in the second."""
        conversation = uuid.uuid7()
        return (
            self.scope(owner, conversation=conversation, turn=1),
            self.scope(owner, conversation=conversation, turn=2),
        )

    async def audit(self, owner: Customer) -> list[AuditLog]:
        rows = await self.session.scalars(
            select(AuditLog).where(AuditLog.on_behalf_of == owner.id).order_by(AuditLog.id)
        )
        return list(rows)

    async def order(
        self,
        owner: Customer,
        kind: str = "delivered",
        *,
        lines: Sequence[tuple[str, int]] = (("AUD-BUDS2", 1),),
        delivered_days_ago: int = 5,
        delivered_at_local: tuple[int, int] = (14, 0),
        late_by: int = 0,
        shipping_cents: int = 0,
        last_scan_days_ago: float = 1,
        note: str | None = None,
        number: str | None = None,
    ) -> Order:
        tz, now = self.policy.tz, self.now
        today = local_date(now, tz)
        number = number or f"PV-{next(_numbers)}"
        address = self.address(owner.full_name)
        status = OrderStatus.SHIPPED
        tracking: TrackingPlan | None = None
        placed = now - timedelta(days=2)
        promised = local_date(placed, tz) + timedelta(days=6)

        def track(status: ShipmentStatus, shipped: datetime, **extra: object) -> TrackingPlan:
            return TrackingPlan(
                carrier="UPS",
                tracking_number=f"1Z{number[3:]}{secrets.token_hex(4).upper()}",
                status=status,
                shipped_at=shipped,
                destination="Springfield, IL",
                hubs=("Columbus, OH", "Indianapolis, IN"),
                **extra,  # type: ignore[arg-type]
            )

        match kind:
            case "pending":
                status, placed = OrderStatus.PENDING, now - timedelta(hours=2)
            case "processing":
                status, placed = OrderStatus.PROCESSING, now - timedelta(hours=20)
            case "cancelled":
                status = OrderStatus.CANCELLED
            case "label_created":
                tracking = track(ShipmentStatus.LABEL_CREATED, now - timedelta(hours=5))
            case "in_transit" | "lost":
                last_scan = now - timedelta(days=last_scan_days_ago)
                placed = last_scan - timedelta(days=2)
                shipment_status = (
                    ShipmentStatus.LOST if kind == "lost" else ShipmentStatus.IN_TRANSIT
                )
                if kind == "lost":
                    last_scan = min(last_scan, now - timedelta(days=7))
                tracking = track(
                    shipment_status, placed + timedelta(hours=20), last_scan_at=last_scan
                )
            case "out_for_delivery":
                out = now - timedelta(hours=3)
                placed = out - timedelta(days=3)
                tracking = track(
                    ShipmentStatus.OUT_FOR_DELIVERY,
                    placed + timedelta(hours=20),
                    last_scan_at=out,
                )
            case "delivered":
                status = OrderStatus.DELIVERED
                delivered = at_local(
                    today - timedelta(days=delivered_days_ago), *delivered_at_local, tz
                )
                promised = local_date(delivered, tz) - timedelta(days=late_by)
                placed = at_local(promised - timedelta(days=6), 9, 0, tz)
                if not late_by:
                    placed = delivered - timedelta(days=4)
                    promised = local_date(placed, tz) + timedelta(days=6)
                tracking = track(
                    ShipmentStatus.DELIVERED,
                    placed + timedelta(hours=20),
                    delivered_at=delivered,
                )
            case _:
                raise ValueError(f"unknown order kind {kind}")

        row = builders.order(
            self.ids,
            owner,
            OrderPlan(
                number=number,
                placed_at=placed,
                status=status,
                lines=list(lines),
                shipping_address=address,
                shipping_cents=shipping_cents,
                promised_delivery_on=promised,
                customer_note=note,
                cancelled_at=placed + timedelta(hours=1) if kind == "cancelled" else None,
                tracking=tracking,
            ),
        )
        self.session.add(row)
        await self.session.flush()
        return row
