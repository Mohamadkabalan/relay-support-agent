"""Builders for store rows: one way to make customers, orders, shipments, returns and refunds.

The seed generator, the demo personas, the tests and (in Phase 4) the eval fixtures all build their
data through these functions, so every order in the system has the same shape: lines priced from
the catalog, a total that adds up, a shipment whose events match its status.

IDs come from an `Ids` source. The seed passes a seeded one so the same seed always produces the
same rows; tests use random UUIDv7s.
"""

import random
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from relay.db.models import Address as AddressRow
from relay.db.models import (
    Customer,
    Order,
    OrderLine,
    Product,
    Refund,
    RefundLine,
    Return,
    ReturnLine,
    Shipment,
    ShipmentEvent,
)
from relay.domain.address import Address
from relay.domain.enums import (
    ActorType,
    OrderStatus,
    RefundChannel,
    RefundLineKind,
    RefundReason,
    RefundStatus,
    ReturnReason,
    ReturnStatus,
    ShipmentStatus,
)
from relay.seed.catalog import BY_SKU, CATALOG

WAREHOUSE = "Secaucus, NJ"
HUBS = (
    "Columbus, OH",
    "Louisville, KY",
    "Memphis, TN",
    "Indianapolis, IN",
    "Chicago, IL",
    "Atlanta, GA",
    "Dallas, TX",
    "Denver, CO",
    "Allentown, PA",
    "Sacramento, CA",
)


class Ids:
    """A source of UUIDv7s. Seeded, it is deterministic; unseeded, it uses `uuid.uuid7`."""

    def __init__(self, rng: random.Random | None = None) -> None:
        self._rng = rng

    def at(self, moment: datetime) -> uuid.UUID:
        if self._rng is None:
            return uuid.uuid7()
        millis = int(moment.timestamp() * 1000) & ((1 << 48) - 1)
        rand = self._rng.getrandbits(80)
        value = (millis << 80) | rand
        return uuid.UUID(int=value, version=7)


def products() -> list[Product]:
    return [
        Product(
            sku=item.sku,
            name=item.name,
            category=item.category,
            kind=item.kind,
            condition=item.condition,
            price_cents=item.price_cents,
            final_sale=item.final_sale,
        )
        for item in CATALOG
    ]


def customer(
    ids: Ids,
    *,
    email: str,
    full_name: str,
    created_at: datetime,
    address: Address,
    phone: str | None = None,
    demo_visitor_id: str | None = None,
) -> Customer:
    row = Customer(
        id=ids.at(created_at),
        email=email.lower(),
        full_name=full_name,
        phone=phone,
        created_at=created_at,
        demo_visitor_id=demo_visitor_id,
    )
    row.addresses = [
        AddressRow(
            id=ids.at(created_at),
            customer_id=row.id,
            recipient_name=address.recipient_name,
            line1=address.line1,
            line2=address.line2,
            city=address.city,
            state=address.state,
            postal_code=address.postal_code,
            country=address.country,
            is_default=True,
        )
    ]
    return row


@dataclass(frozen=True, slots=True)
class TrackingPlan:
    """What happened to a package. `tracking_events` turns it into carrier scans."""

    carrier: str
    tracking_number: str
    status: ShipmentStatus
    shipped_at: datetime  # when the label was created
    delivered_at: datetime | None = None
    last_scan_at: datetime | None = None  # for packages still moving or stuck
    eta: date | None = None
    destination: str = "Springfield, IL"
    hubs: tuple[str, ...] = ("Columbus, OH",)


@dataclass(slots=True)
class OrderPlan:
    number: str
    placed_at: datetime
    status: OrderStatus
    lines: Sequence[tuple[str, int]]  # (sku, quantity)
    shipping_address: Address
    shipping_cents: int = 599
    promised_delivery_on: date | None = None
    payment_brand: str = "Visa"
    payment_last4: str = "4242"
    customer_note: str | None = None
    cancelled_at: datetime | None = None
    tracking: TrackingPlan | None = None
    extra_events: list[tuple[datetime, str, str, str | None]] = field(default_factory=list)


def order(ids: Ids, owner: Customer, plan: OrderPlan) -> Order:
    lines = [
        OrderLine(
            id=ids.at(plan.placed_at),
            line_no=index,
            sku=sku,
            name=BY_SKU[sku].name,
            unit_price_cents=BY_SKU[sku].price_cents,
            quantity=quantity,
        )
        for index, (sku, quantity) in enumerate(plan.lines, start=1)
    ]
    subtotal = sum(line.unit_price_cents * line.quantity for line in lines)
    row = Order(
        id=ids.at(plan.placed_at),
        number=plan.number,
        customer_id=owner.id,
        status=plan.status,
        placed_at=plan.placed_at,
        promised_delivery_on=plan.promised_delivery_on,
        shipping_address=plan.shipping_address.model_dump(mode="json"),
        payment_brand=plan.payment_brand,
        payment_last4=plan.payment_last4,
        shipping_cents=plan.shipping_cents,
        total_cents=subtotal + plan.shipping_cents,
        customer_note=plan.customer_note,
        cancelled_at=plan.cancelled_at,
    )
    row.lines = lines
    for line in lines:
        line.order_id = row.id
    if plan.tracking is not None:
        row.shipment = shipment(ids, row, plan.tracking, plan.extra_events)
    return row


def shipment(
    ids: Ids,
    owner: Order,
    plan: TrackingPlan,
    extra_events: Sequence[tuple[datetime, str, str, str | None]] = (),
) -> Shipment:
    row = Shipment(
        id=ids.at(plan.shipped_at),
        order_id=owner.id,
        carrier=plan.carrier,
        tracking_number=plan.tracking_number,
        status=plan.status,
        shipped_at=plan.shipped_at,
        delivered_at=plan.delivered_at,
        eta=plan.eta,
    )
    events = sorted([*tracking_events(plan), *extra_events], key=lambda event: event[0])
    row.events = [
        ShipmentEvent(
            id=ids.at(occurred_at),
            shipment_id=row.id,
            occurred_at=occurred_at,
            code=code,
            description=description,
            location=location,
        )
        for occurred_at, code, description, location in events
    ]
    return row


def tracking_events(plan: TrackingPlan) -> list[tuple[datetime, str, str, str | None]]:
    """Carrier scans consistent with the plan's status, oldest first."""
    events: list[tuple[datetime, str, str, str | None]] = [
        (plan.shipped_at, "label_created", "Shipping label created", WAREHOUSE),
    ]
    if plan.status is ShipmentStatus.LABEL_CREATED:
        return events

    picked_up = plan.shipped_at + timedelta(hours=5)
    events.append((picked_up, "picked_up", "Picked up by carrier", WAREHOUSE))
    match plan.status:
        case ShipmentStatus.DELIVERED:
            final = _required(plan.delivered_at, "delivered_at") - timedelta(hours=10)
        case ShipmentStatus.OUT_FOR_DELIVERY:
            final = _required(plan.last_scan_at, "last_scan_at") - timedelta(hours=6)
        case _:
            final = _required(plan.last_scan_at, "last_scan_at")

    # Hub scans roughly every 18 hours until the final leg.
    moment = picked_up + timedelta(hours=8)
    hub = plan.hubs[0]
    for hub in plan.hubs:
        if moment >= final:
            break
        events.append((moment, "arrived_at_facility", "Arrived at carrier facility", hub))
        if moment + timedelta(hours=4) < final:
            departed = moment + timedelta(hours=4)
            events.append((departed, "departed_facility", "Departed carrier facility", hub))
        moment += timedelta(hours=18)

    match plan.status:
        case ShipmentStatus.DELIVERED:
            delivered_at = _required(plan.delivered_at, "delivered_at")
            events += [
                (final, "arrived_at_facility", "Arrived at local facility", plan.destination),
                (
                    delivered_at - timedelta(hours=4),
                    "out_for_delivery",
                    "Out for delivery",
                    plan.destination,
                ),
                (
                    delivered_at,
                    "delivered",
                    "Delivered, left at front door",
                    plan.destination,
                ),
            ]
        case ShipmentStatus.OUT_FOR_DELIVERY:
            last_scan = _required(plan.last_scan_at, "last_scan_at")
            events += [
                (final, "arrived_at_facility", "Arrived at local facility", plan.destination),
                (last_scan, "out_for_delivery", "Out for delivery", plan.destination),
            ]
        case _:
            last_scan = _required(plan.last_scan_at, "last_scan_at")
            if events[-1][0] < last_scan:
                events.append((last_scan, "in_transit", "In transit to destination", hub))
            if plan.status is ShipmentStatus.LOST:
                events.append(
                    (
                        last_scan + timedelta(days=6),
                        "lost",
                        "Package could not be located. Carrier investigation closed.",
                        None,
                    )
                )
    return events


def _required(value: datetime | None, name: str) -> datetime:
    if value is None:
        raise ValueError(f"this shipment status needs {name}")
    return value


def return_(
    ids: Ids,
    owner: Order,
    *,
    rma_number: str,
    created_at: datetime,
    status: ReturnStatus,
    reason: ReturnReason,
    lines: dict[int, int],
    details: str | None = None,
    created_by: ActorType = ActorType.AGENT,
) -> Return:
    by_no = {line.line_no: line for line in owner.lines}
    row = Return(
        id=ids.at(created_at),
        rma_number=rma_number,
        order_id=owner.id,
        status=status,
        reason=reason,
        details=details,
        label_url=f"https://returns.pixelvolt.example/labels/{rma_number}.pdf",
        created_at=created_at,
        created_by_type=created_by,
    )
    row.lines = [
        ReturnLine(
            id=ids.at(created_at), return_id=row.id, order_line_id=by_no[no].id, quantity=qty
        )
        for no, qty in sorted(lines.items())
    ]
    return row


def refund(
    ids: Ids,
    owner: Order,
    *,
    refund_number: str,
    issued_at: datetime,
    reason: RefundReason,
    channel: RefundChannel,
    lines: dict[int, int] | None = None,
    shipping: bool = False,
    return_id: uuid.UUID | None = None,
) -> Refund:
    by_no = {line.line_no: line for line in owner.lines}
    refund_lines = [
        RefundLine(
            id=ids.at(issued_at),
            kind=RefundLineKind.ITEM,
            order_line_id=by_no[no].id,
            quantity=qty,
            amount_cents=by_no[no].unit_price_cents * qty,
        )
        for no, qty in sorted((lines or {}).items())
        if by_no[no].unit_price_cents > 0
    ]
    if shipping and owner.shipping_cents > 0:
        refund_lines.append(
            RefundLine(
                id=ids.at(issued_at),
                kind=RefundLineKind.SHIPPING,
                amount_cents=owner.shipping_cents,
            )
        )
    row = Refund(
        id=ids.at(issued_at),
        refund_number=refund_number,
        order_id=owner.id,
        reason=reason,
        channel=channel,
        amount_cents=sum(line.amount_cents for line in refund_lines),
        status=RefundStatus.ISSUED,
        issued_at=issued_at,
        return_id=return_id,
    )
    for line in refund_lines:
        line.refund_id = row.id
    row.lines = refund_lines
    return row


def at_local(day: date, hour: int, minute: int, tz: ZoneInfo) -> datetime:
    """A store-local wall-clock time on `day`, as an aware datetime."""
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz)
