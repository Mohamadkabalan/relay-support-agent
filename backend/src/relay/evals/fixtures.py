"""Applying a scenario's fixtures to its trial database, relative to the frozen eval clock.

Fixture customers get their own addresses and emails, and fixture orders get numbers in the
PV-9xxxxx range the base seed leaves free, so nothing collides with seeded data. The same
scenario always produces the same order numbers and line numbers, which the policy re-check
(`grading.recheck_writes`) relies on when it rebuilds the pre-trial state in a fresh clone.
"""

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from relay.clock import local_date
from relay.db.models import Customer, Operator
from relay.domain.address import Address
from relay.domain.enums import OrderStatus, ShipmentStatus
from relay.domain.policy_config import PolicyConfig
from relay.evals.scenario import OrderFixture, Scenario
from relay.seed import builders
from relay.seed.builders import Ids, OrderPlan, TrackingPlan, at_local

STREETS = ("Maple Avenue", "Harbor Road", "Cedar Lane", "Lakeview Drive", "Mill Street")
OPERATOR_EMAIL = "eval.operator@relay.example"


@dataclass
class Applied:
    """What the fixtures created, for filling `{ref.field}` and for the graders."""

    values: dict[str, dict[str, str]] = field(default_factory=dict)
    customer_ids: dict[str, uuid.UUID] = field(default_factory=dict)
    order_numbers: dict[str, str] = field(default_factory=dict)  # ref -> number
    emails: dict[str, str] = field(default_factory=dict)  # customer ref -> email
    operator_id: uuid.UUID | None = None
    # Things that identify another customer (V2): names, emails, order numbers, street lines.
    foreign_markers: set[str] = field(default_factory=set)

    def session_customer(self, scenario: Scenario) -> uuid.UUID:
        return self.customer_ids[scenario.session_customer]


async def apply_fixtures(
    session: AsyncSession, scenario: Scenario, now: datetime, policy: PolicyConfig
) -> Applied:
    applied = Applied()
    ids = Ids()
    customers: dict[str, Customer] = {}
    addresses: dict[str, Address] = {}
    for index, person in enumerate(scenario.fixtures.customers):
        email = person.email or f"{person.ref}.{scenario.id}@eval.example"
        address = Address(
            recipient_name=person.name,
            line1=f"{120 + 7 * index} {STREETS[index % len(STREETS)]}",
            city="Springfield",
            state="IL",
            postal_code="62701",
        )
        row = builders.customer(
            ids,
            email=email,
            full_name=person.name,
            created_at=now - timedelta(days=400),
            address=address,
        )
        session.add(row)
        customers[person.ref] = row
        addresses[person.ref] = address
        applied.emails[person.ref] = email
        applied.values[person.ref] = {
            "name": person.name,
            "first_name": person.name.split()[0],
            "email": email,
        }
        if person.ref != scenario.session_customer:
            applied.foreign_markers |= {person.name, email, address.line1}

    for index, spec in enumerate(scenario.fixtures.orders):
        number = spec.number or f"PV-{900001 + index}"
        plan = order_plan(spec, number, addresses[spec.customer], now, policy)
        session.add(builders.order(ids, customers[spec.customer], plan))
        applied.order_numbers[spec.ref] = number
        tracking = plan.tracking
        applied.values[spec.ref] = {
            "number": number,
            "tracking_number": tracking.tracking_number if tracking else "",
            "carrier": tracking.carrier if tracking else "",
        }
        if spec.customer != scenario.session_customer:
            applied.foreign_markers.add(number)

    operator = Operator(email=OPERATOR_EMAIL, display_name="Eval Operator")
    session.add(operator)
    await session.flush()
    applied.customer_ids = {ref: row.id for ref, row in customers.items()}
    applied.operator_id = operator.id
    return applied


def order_plan(
    spec: OrderFixture, number: str, address: Address, now: datetime, policy: PolicyConfig
) -> OrderPlan:
    """The order's timeline for its status, the way the base seed builds real orders."""
    tz = policy.tz
    today = local_date(now, tz)
    status = OrderStatus.SHIPPED
    tracking: TrackingPlan | None = None
    placed = now - timedelta(days=2)
    promised = local_date(placed, tz) + timedelta(days=policy.shipping.standard_promise_days)
    cancelled_at = None

    def track(state: ShipmentStatus, shipped: datetime, **extra: object) -> TrackingPlan:
        return TrackingPlan(
            carrier="UPS",
            tracking_number=f"1Z{number[3:]}EV{len(spec.lines):02d}",
            status=state,
            shipped_at=shipped,
            destination=f"{address.city}, {address.state}",
            hubs=("Columbus, OH", "Indianapolis, IN"),
            **extra,  # type: ignore[arg-type]
        )

    match spec.status:
        case "pending":
            status, placed = OrderStatus.PENDING, now - timedelta(hours=2)
        case "processing":
            status, placed = OrderStatus.PROCESSING, now - timedelta(hours=20)
        case "cancelled":
            status = OrderStatus.CANCELLED
            cancelled_at = placed + timedelta(hours=1)
        case "label_created":
            tracking = track(ShipmentStatus.LABEL_CREATED, now - timedelta(hours=5))
        case "in_transit" | "lost":
            last_scan = now - timedelta(days=spec.last_scan_days_ago)
            if spec.status == "lost":
                last_scan = min(last_scan, now - timedelta(days=7))
            placed = last_scan - timedelta(days=2)
            promised = local_date(placed, tz) + timedelta(
                days=policy.shipping.standard_promise_days
            )
            state = ShipmentStatus.LOST if spec.status == "lost" else ShipmentStatus.IN_TRANSIT
            tracking = track(state, placed + timedelta(hours=20), last_scan_at=last_scan)
        case "out_for_delivery":
            out = now - timedelta(hours=3)
            placed = out - timedelta(days=3)
            tracking = track(
                ShipmentStatus.OUT_FOR_DELIVERY, placed + timedelta(hours=20), last_scan_at=out
            )
        case "delivered":
            status = OrderStatus.DELIVERED
            delivered = at_local(today - timedelta(days=spec.delivered_days_ago), 14, 0, tz)
            if spec.late_by_days:
                promised = local_date(delivered, tz) - timedelta(days=spec.late_by_days)
                placed = at_local(promised - timedelta(days=6), 9, 0, tz)
            else:
                placed = delivered - timedelta(days=4)
                promised = local_date(placed, tz) + timedelta(days=6)
            tracking = track(
                ShipmentStatus.DELIVERED, placed + timedelta(hours=20), delivered_at=delivered
            )

    extra_events = [
        (now - timedelta(days=event.days_ago), event.code, event.description, event.location)
        for event in spec.carrier_events
    ]
    return OrderPlan(
        number=number,
        placed_at=placed,
        status=status,
        lines=[(line.sku, line.quantity) for line in spec.lines],
        shipping_address=address,
        shipping_cents=spec.shipping_cents,
        promised_delivery_on=promised,
        customer_note=spec.customer_note,
        cancelled_at=cancelled_at,
        tracking=tracking,
        extra_events=extra_events,
    )
