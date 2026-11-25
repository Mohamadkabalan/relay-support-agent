"""The deterministic store generator.

`generate(seed, now, policy)` returns ORM rows for ~500 customers and ~2,000 orders placed over the
last 180 days. Everything is relative to `now`, and every random choice comes from generators seeded
with `seed`, so the same inputs always produce the same rows (and the same data hash).

Order statuses follow the shares in PLAN.md §8. Each order's timeline is built backwards from its
status, so a delivered order really was placed, shipped and scanned before it arrived.
"""

import random
import unicodedata
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from faker import Faker

from relay.clock import local_date
from relay.db.models import Customer, Order, Product, Refund, Return
from relay.domain.address import Address
from relay.domain.enums import (
    ActorType,
    OrderStatus,
    ProductCondition,
    ProductKind,
    RefundChannel,
    RefundReason,
    ReturnReason,
    ReturnStatus,
    ShipmentStatus,
)
from relay.domain.numbers import refund_number, rma_number
from relay.domain.policy_config import PolicyConfig
from relay.seed import builders
from relay.seed.builders import HUBS, Ids, OrderPlan, TrackingPlan, at_local
from relay.seed.catalog import BY_SKU, CATALOG, CatalogItem
from relay.seed.personas import PERSONA_EMAILS, build_personas

GENERATOR_VERSION = "1"

# Order numbers: random orders PV-100000 to PV-799999, personas PV-8xxxxx, eval fixtures PV-9xxxxx.
RANDOM_ORDER_NUMBERS = range(100000, 800000)

STATUS_SHARES = (
    ("delivered", 0.72),
    ("in_transit", 0.075),
    ("label_created", 0.01),
    ("stalled", 0.01),
    ("lost", 0.005),
    ("out_for_delivery", 0.02),
    ("processing", 0.06),
    ("pending", 0.03),
    ("cancelled", 0.03),
)

CATEGORY_WEIGHTS = {
    "accessories": 6,
    "audio": 4,
    "smart_home": 3,
    "phones": 2,
    "wearables": 2,
    "computers": 2,
    "tablets": 2,
    "cameras": 1.5,
    "gaming": 1.5,
    "gift_cards_and_plans": 0.8,
}

CARD_BRANDS = (("Visa", 0.55), ("Mastercard", 0.30), ("Amex", 0.10), ("Discover", 0.05))
CARRIERS = (("UPS", 0.45), ("FedEx", 0.30), ("USPS", 0.25))
EMAIL_DOMAINS = ("example.com", "example.net", "example.org")
AREA_CODES = ("212", "312", "415", "512", "614", "617", "702", "720", "813", "919", "206", "404")

NOTES = (
    "Please leave the package with the building concierge.",
    "Gift for my dad's birthday. Please don't include the receipt.",
    "Ring the doorbell twice, the dog is friendly.",
    "Leave it at the side door if nobody answers.",
    "Happy anniversary! Love, J.",
    "Deliver after 5 pm on weekdays if possible.",
    "Front desk closes at 6 pm.",
    "Use the gate code 4417 for the parking lot.",
)

RETURN_DETAILS = {
    ReturnReason.CHANGED_MIND: "Decided to keep my old one.",
    ReturnReason.NO_LONGER_NEEDED: "Bought it for a trip that got cancelled.",
    ReturnReason.WRONG_ITEM: "Received a different color than I ordered.",
    ReturnReason.NOT_AS_DESCRIBED: "Battery life is much shorter than advertised.",
    ReturnReason.DAMAGED_OR_DEFECTIVE: "Stopped charging after two days.",
    ReturnReason.OTHER: "Ordered two by mistake.",
}


@dataclass(slots=True)
class SeedData:
    products: list[Product]
    customers: list[Customer]
    orders: list[Order]
    returns: list[Return] = field(default_factory=list)
    refunds: list[Refund] = field(default_factory=list)
    last_rma: int = 0
    last_refund: int = 0


def generate(
    *,
    seed: int,
    now: datetime,
    policy: PolicyConfig,
    customers: int = 500,
    orders: int = 2000,
) -> SeedData:
    return _Generator(seed, now, policy).run(customers, orders)


class _Generator:
    def __init__(self, seed: int, now: datetime, policy: PolicyConfig) -> None:
        self.rng = random.Random(seed)
        self.ids = Ids(random.Random(seed + 1))
        self.fake = Faker("en_US")
        self.fake.seed_instance(seed)
        self.now = now
        self.policy = policy
        self.tz: ZoneInfo = policy.tz
        self.today = local_date(now, self.tz)
        self.rma_counter = 100000
        self.refund_counter = 10000
        self.cards: dict[str, list[tuple[str, str]]] = {}
        self.addresses: dict[str, Address] = {}
        self.order_info: dict[str, _OrderInfo] = {}

    def run(self, n_customers: int, n_orders: int) -> SeedData:
        products = builders.products()
        people = self._customers(n_customers)
        orders = self._orders(people, n_orders)
        data = SeedData(products=products, customers=people, orders=orders)
        self._returns(data)
        self._refunds(data)
        self._velocity_customers(data)

        personas = build_personas(self.ids, self.now, self.policy)
        data.customers += personas.customers
        data.orders += personas.orders
        data.last_rma = self.rma_counter
        data.last_refund = self.refund_counter
        return data

    # ------------------------------------------------------------ customers

    def _customers(self, count: int) -> list[Customer]:
        people: list[Customer] = []
        used = set(PERSONA_EMAILS)
        for index in range(count):
            first, last = self.fake.first_name(), self.fake.last_name()
            email = self._email(first, last, index, used)
            used.add(email)
            state = self.fake.state_abbr(
                include_territories=False, include_freely_associated_states=False
            )
            address = Address(
                recipient_name=f"{first} {last}",
                line1=self.fake.street_address(),
                city=self.fake.city(),
                state=state,
                postal_code=self.fake.zipcode_in_state(state),
            )
            created = self.now - timedelta(
                days=self.rng.randint(30, 1800), seconds=self.rng.randint(0, 86399)
            )
            person = builders.customer(
                self.ids,
                email=email,
                full_name=f"{first} {last}",
                created_at=created,
                address=address,
                phone=f"+1-{self.rng.choice(AREA_CODES)}-555-01{self.rng.randint(0, 99):02d}",
            )
            self.addresses[email] = address
            self.cards[email] = [
                (_weighted(self.rng, CARD_BRANDS), f"{self.rng.randint(0, 9999):04d}")
                for _ in range(self.rng.choice((1, 1, 2)))
            ]
            people.append(person)
        return people

    def _email(self, first: str, last: str, index: int, used: set[str]) -> str:
        local = f"{_ascii(first)}.{_ascii(last)}"
        domain = EMAIL_DOMAINS[index % len(EMAIL_DOMAINS)]
        email = f"{local}@{domain}"
        suffix = 2
        while email in used:
            email = f"{local}{suffix}@{domain}"
            suffix += 1
        return email

    # ------------------------------------------------------------ orders

    def _orders(self, people: list[Customer], count: int) -> list[Order]:
        weights = [self.rng.lognormvariate(0, 0.9) for _ in people]
        owners = list(people) + self.rng.choices(people, weights=weights, k=count - len(people))
        self.rng.shuffle(owners)
        numbers = self.rng.sample(RANDOM_ORDER_NUMBERS, count)
        statuses = self.rng.choices(
            [name for name, _ in STATUS_SHARES],
            weights=[share for _, share in STATUS_SHARES],
            k=count,
        )
        return [
            self._order(owner, f"PV-{number}", status)
            for owner, number, status in zip(owners, numbers, statuses, strict=True)
        ]

    def _order(self, owner: Customer, number: str, kind: str) -> Order:
        rng = self.rng
        items = self._pick_items()
        subtotal = sum(BY_SKU[sku].price_cents * qty for sku, qty in items)
        rates = self.policy.shipping
        express = rng.random() < 0.15
        if express:
            shipping, promise_days = rates.express_cents, rates.express_promise_days
        else:
            free = subtotal >= rates.free_standard_from_cents
            shipping, promise_days = (
                (0 if free else rates.standard_cents),
                rates.standard_promise_days,
            )
        brand, last4 = rng.choice(self.cards[owner.email])
        address = self.addresses[owner.email]
        note = rng.choice(NOTES) if rng.random() < 0.06 else None

        placed, status, tracking, cancelled_at = self._timeline(kind, express, address)
        plan = OrderPlan(
            number=number,
            placed_at=placed,
            status=status,
            lines=items,
            shipping_address=address,
            shipping_cents=shipping,
            promised_delivery_on=local_date(placed, self.tz) + timedelta(days=promise_days),
            payment_brand=brand,
            payment_last4=last4,
            customer_note=note,
            cancelled_at=cancelled_at,
            tracking=tracking,
        )
        if tracking is not None and tracking.delivered_at is not None:
            self._maybe_late(plan, tracking, tracking.delivered_at, promise_days)
        row = builders.order(self.ids, owner, plan)
        self.order_info[number] = _OrderInfo(kind=kind, express=express)
        return row

    def _pick_items(self) -> list[tuple[str, int]]:
        rng = self.rng
        n_lines = rng.choices((1, 2, 3, 4), weights=(45, 30, 17, 8))[0]
        weights = [CATEGORY_WEIGHTS[item.category] for item in CATALOG]
        chosen: list[CatalogItem] = []
        while len(chosen) < n_lines:
            item = rng.choices(CATALOG, weights=weights)[0]
            if item not in chosen:
                chosen.append(item)
        return [
            (item.sku, rng.choice((1, 1, 1, 2, 3)) if item.price_cents < 6000 else 1)
            for item in chosen
        ]

    def _timeline(
        self, kind: str, express: bool, address: Address
    ) -> tuple[datetime, OrderStatus, TrackingPlan | None, datetime | None]:
        rng, now = self.rng, self.now
        hours = timedelta(hours=1)
        days = timedelta(days=1)
        match kind:
            case "pending":
                return now - rng.uniform(0.3, 10) * hours, OrderStatus.PENDING, None, None
            case "processing":
                placed = now - rng.uniform(10, 60) * hours
                return placed, OrderStatus.PROCESSING, None, None
            case "cancelled":
                placed = now - rng.uniform(1, 180) * days
                cancelled = placed + rng.uniform(1, 20) * hours
                return placed, OrderStatus.CANCELLED, None, min(cancelled, now - hours)
            case "label_created":
                shipped = now - rng.uniform(2, 20) * hours
                placed = shipped - rng.uniform(12, 30) * hours
                return (
                    placed,
                    OrderStatus.SHIPPED,
                    self._tracking(ShipmentStatus.LABEL_CREATED, shipped, address),
                    None,
                )
            case "in_transit":
                last_scan = now - rng.uniform(1, 60) * hours
                if rng.random() < 0.15:  # slow but not lost yet: 3 to 6 days without a scan
                    last_scan = now - rng.uniform(3, 6.5) * days
                shipped = last_scan - rng.uniform(0.5, 2.5) * days
                placed = shipped - rng.uniform(8, 36) * hours
                return (
                    placed,
                    OrderStatus.SHIPPED,
                    self._tracking(
                        ShipmentStatus.IN_TRANSIT, shipped, address, last_scan=last_scan
                    ),
                    None,
                )
            case "stalled":
                last_scan = now - rng.uniform(7.5, 20) * days
                shipped = last_scan - rng.uniform(1, 3) * days
                placed = shipped - rng.uniform(8, 36) * hours
                return (
                    placed,
                    OrderStatus.SHIPPED,
                    self._tracking(
                        ShipmentStatus.IN_TRANSIT, shipped, address, last_scan=last_scan
                    ),
                    None,
                )
            case "lost":
                last_scan = now - rng.uniform(10, 30) * days
                shipped = last_scan - rng.uniform(1, 3) * days
                placed = shipped - rng.uniform(8, 36) * hours
                return (
                    placed,
                    OrderStatus.SHIPPED,
                    self._tracking(ShipmentStatus.LOST, shipped, address, last_scan=last_scan),
                    None,
                )
            case "out_for_delivery":
                out = now - rng.uniform(1, 5) * hours
                shipped = out - rng.uniform(1.5, 4) * days
                placed = shipped - rng.uniform(8, 36) * hours
                return (
                    placed,
                    OrderStatus.SHIPPED,
                    self._tracking(
                        ShipmentStatus.OUT_FOR_DELIVERY, shipped, address, last_scan=out
                    ),
                    None,
                )
            case _:
                return self._delivered_timeline(express, address)

    def _delivered_timeline(
        self, express: bool, address: Address
    ) -> tuple[datetime, OrderStatus, TrackingPlan, None]:
        rng = self.rng
        days_ago = rng.randint(0, 175)
        delivered = at_local(
            self.today - timedelta(days=days_ago), rng.randint(10, 19), rng.randint(0, 59), self.tz
        )
        if delivered > self.now - timedelta(hours=1):
            delivered -= timedelta(days=1)
        transit = rng.randint(1, 2) if express else rng.randint(2, 5)
        shipped = delivered - timedelta(days=transit, hours=rng.randint(2, 8))
        placed = shipped - timedelta(hours=rng.uniform(8, 36))
        tracking = self._tracking(ShipmentStatus.DELIVERED, shipped, address, delivered=delivered)
        return placed, OrderStatus.DELIVERED, tracking, None

    def _maybe_late(
        self, plan: OrderPlan, tracking: TrackingPlan, delivered: datetime, promise_days: int
    ) -> None:
        """About 7% of deliveries arrive two or more days after the promised date.

        The delivery stays where it is; the order moves earlier, so the promise (placed date plus
        the shipping method's days) falls `late_by` days before the delivery.
        """
        rng = self.rng
        if rng.random() >= 0.07:
            return
        promised = local_date(delivered, self.tz) - timedelta(days=rng.randint(2, 6))
        placed = at_local(
            promised - timedelta(days=promise_days), rng.randint(7, 22), rng.randint(0, 59), self.tz
        )
        plan.placed_at = placed
        plan.promised_delivery_on = promised
        plan.tracking = replace(
            tracking, shipped_at=placed + timedelta(hours=rng.uniform(8, 36)), eta=promised
        )

    def _tracking(
        self,
        status: ShipmentStatus,
        shipped: datetime,
        address: Address,
        *,
        delivered: datetime | None = None,
        last_scan: datetime | None = None,
    ) -> TrackingPlan:
        carrier = _weighted(self.rng, CARRIERS)
        eta_base = delivered or (shipped + timedelta(days=self.rng.randint(2, 5)))
        return TrackingPlan(
            carrier=carrier,
            tracking_number=self._tracking_number(carrier),
            status=status,
            shipped_at=shipped,
            delivered_at=delivered,
            last_scan_at=last_scan,
            eta=local_date(eta_base, self.tz),
            destination=f"{address.city}, {address.state}",
            hubs=tuple(self.rng.sample(HUBS, k=self.rng.randint(2, 4))),
        )

    def _tracking_number(self, carrier: str) -> str:
        rng = self.rng
        if carrier == "UPS":
            return "1Z" + "".join(rng.choices("0123456789ABCDEFGHJKLMNPRSTUVWXYZ", k=16))
        if carrier == "FedEx":
            return "".join(rng.choices("0123456789", k=12))
        return "9400" + "".join(rng.choices("0123456789", k=18))

    # ------------------------------------------------------------ returns and refunds

    def _returns(self, data: SeedData) -> None:
        """Returns on about 8% of delivered orders, created inside the window, in every status."""
        rng = self.rng
        for order in data.orders:
            delivered_at = order.shipment.delivered_at if order.shipment else None
            if delivered_at is None or rng.random() >= 0.08:
                continue
            candidates = [line for line in order.lines if _returnable(BY_SKU[line.sku])]
            if not candidates:
                continue
            line = rng.choice(candidates)
            window = self._window_days(BY_SKU[line.sku])
            delivered_on = local_date(delivered_at, self.tz)
            last_day = min(delivered_on + timedelta(days=window), self.today)
            span = (last_day - delivered_on).days
            if span < 1:
                continue
            created_on = delivered_on + timedelta(days=rng.randint(1, span))
            created = at_local(created_on, rng.randint(8, 21), rng.randint(0, 59), self.tz)
            if created >= self.now:
                created = self.now - timedelta(hours=1)
            age = (self.now - created).days
            if rng.random() < 0.1:
                status = ReturnStatus.CANCELLED
            elif age < 3:
                status = ReturnStatus.LABEL_ISSUED
            elif age < 6:
                status = ReturnStatus.IN_TRANSIT
            elif age < 10:
                status = ReturnStatus.RECEIVED
            else:
                status = ReturnStatus.COMPLETED
            reason = rng.choice(list(RETURN_DETAILS))
            self.rma_counter += 1
            return_row = builders.return_(
                self.ids,
                order,
                rma_number=rma_number(self.rma_counter),
                created_at=created,
                status=status,
                reason=reason,
                lines={line.line_no: rng.randint(1, line.quantity)},
                details=RETURN_DETAILS[reason] if rng.random() < 0.5 else None,
                created_by=ActorType.AGENT if rng.random() < 0.6 else ActorType.OPERATOR,
            )
            data.returns.append(return_row)
            self.order_info[order.number].has_return = True
            if status is ReturnStatus.COMPLETED:
                qty = return_row.lines[0].quantity
                self.refund_counter += 1
                data.refunds.append(
                    builders.refund(
                        self.ids,
                        order,
                        refund_number=refund_number(self.refund_counter),
                        issued_at=created + timedelta(days=rng.randint(8, 10)),
                        reason=RefundReason.RETURN_RECEIVED,
                        channel=RefundChannel.WAREHOUSE,
                        lines={line.line_no: qty},
                        return_id=return_row.id,
                    )
                )

    def _refunds(self, data: SeedData) -> None:
        """Refunds without a return on about 5% of orders: damaged items, late and lost packages."""
        rng = self.rng
        for order in data.orders:
            info = self.order_info[order.number]
            if info.has_return or info.refunded:
                continue
            shipment = order.shipment
            if order.status is OrderStatus.DELIVERED and shipment and shipment.delivered_at:
                delivered_on = local_date(shipment.delivered_at, self.tz)
                late_by = (
                    (delivered_on - order.promised_delivery_on).days
                    if order.promised_delivery_on
                    else 0
                )
                if late_by >= 2 and order.shipping_cents > 0 and rng.random() < 0.5:
                    issued = shipment.delivered_at + timedelta(hours=rng.uniform(4, 72))
                    self._add_refund(data, order, issued, RefundReason.LATE_DELIVERY, shipping=True)
                elif rng.random() < 0.04:
                    line = rng.choice(order.lines)
                    window = self._window_days(BY_SKU[line.sku])
                    span = min(window, (self.today - delivered_on).days)
                    if span < 1 or line.unit_price_cents == 0:
                        continue
                    issued_on = delivered_on + timedelta(days=rng.randint(1, span))
                    issued = at_local(issued_on, rng.randint(9, 20), rng.randint(0, 59), self.tz)
                    self._add_refund(
                        data,
                        order,
                        min(issued, self.now - timedelta(minutes=30)),
                        RefundReason.DAMAGED_OR_DEFECTIVE,
                        lines={line.line_no: 1},
                    )
            elif (
                shipment is not None
                and shipment.status is ShipmentStatus.LOST
                and rng.random() < 0.5
            ):
                issued = shipment.events[-1].occurred_at + timedelta(hours=rng.uniform(2, 48))
                self._add_refund(
                    data,
                    order,
                    min(issued, self.now - timedelta(minutes=30)),
                    RefundReason.LOST_IN_TRANSIT,
                    lines={line.line_no: line.quantity for line in order.lines},
                    shipping=True,
                    channel=RefundChannel.STAFF,
                )

    def _velocity_customers(self, data: SeedData) -> None:
        """A few customers with two agent refunds in the last 90 days, so REF-6 has work to do."""
        rng = self.rng
        recent: dict[str, list[Order]] = {}
        for order in data.orders:
            info = self.order_info[order.number]
            shipment = order.shipment
            if (
                order.status is OrderStatus.DELIVERED
                and shipment is not None
                and shipment.delivered_at is not None
                and not info.has_return
                and not info.refunded
                and self.now - shipment.delivered_at < timedelta(days=60)
                and (self.today - local_date(shipment.delivered_at, self.tz)).days >= 2
            ):
                recent.setdefault(str(order.customer_id), []).append(order)
        eligible = sorted(
            (orders for orders in recent.values() if len(orders) >= 2),
            key=lambda orders: orders[0].number,
        )
        for orders in rng.sample(eligible, k=min(4, len(eligible))):
            for order in sorted(orders, key=lambda row: row.number)[:2]:
                delivered_at = order.shipment.delivered_at if order.shipment else None
                if delivered_at is None:  # pragma: no cover - filtered above
                    continue
                line = max(order.lines, key=lambda row: row.unit_price_cents)
                issued = delivered_at + timedelta(days=1, hours=rng.randint(0, 8))
                self._add_refund(
                    data,
                    order,
                    min(issued, self.now - timedelta(minutes=30)),
                    RefundReason.DAMAGED_OR_DEFECTIVE,
                    lines={line.line_no: 1},
                    channel=RefundChannel.AGENT,
                )

    def _add_refund(
        self,
        data: SeedData,
        order: Order,
        issued: datetime,
        reason: RefundReason,
        *,
        lines: dict[int, int] | None = None,
        shipping: bool = False,
        channel: RefundChannel | None = None,
    ) -> None:
        self.refund_counter += 1
        data.refunds.append(
            builders.refund(
                self.ids,
                order,
                refund_number=refund_number(self.refund_counter),
                issued_at=issued,
                reason=reason,
                channel=channel
                or _weighted(self.rng, ((RefundChannel.AGENT, 0.5), (RefundChannel.STAFF, 0.5))),
                lines=lines,
                shipping=shipping,
            )
        )
        self.order_info[order.number].refunded = True

    def _window_days(self, item: CatalogItem) -> int:
        if item.condition is ProductCondition.REFURBISHED:
            return self.policy.returns.window_days_refurbished
        return self.policy.returns.window_days


@dataclass(slots=True)
class _OrderInfo:
    kind: str
    express: bool
    has_return: bool = False
    refunded: bool = False


def _returnable(item: CatalogItem) -> bool:
    return item.kind is ProductKind.PHYSICAL and not item.final_sale


def _weighted[T](rng: random.Random, options: tuple[tuple[T, float], ...]) -> T:
    return rng.choices([value for value, _ in options], weights=[w for _, w in options])[0]


def _ascii(name: str) -> str:
    folded = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return "".join(ch for ch in folded.lower() if ch.isalnum())
