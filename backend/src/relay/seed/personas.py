"""The six demo personas (PLAN.md §8), each set up to show one policy path.

All dates are relative to `now`, so the personas work whenever the demo is reset. `relay seed
--verify` evaluates each persona's scenario with the real policy engine and fails if the outcome
isn't the one the persona exists to demonstrate.
"""

from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from relay.clock import local_date
from relay.db.models import Customer, Order
from relay.domain.address import Address
from relay.domain.enums import OrderStatus, RefundReason, ReturnReason, ShipmentStatus
from relay.domain.policy import (
    AddressChangeRequest,
    LineRequest,
    Outcome,
    RefundRequest,
    Request,
    ReturnRequest,
)
from relay.domain.policy_config import PolicyConfig
from relay.seed import builders
from relay.seed.builders import Ids, OrderPlan, TrackingPlan, at_local


@dataclass(frozen=True, slots=True)
class PersonaCheck:
    """A request the persona will make, and the outcome the policy must give it."""

    order_number: str
    request: Request
    expected: Outcome


@dataclass(frozen=True, slots=True)
class Persona:
    key: str
    full_name: str
    email: str
    story: str
    checks: tuple[PersonaCheck, ...]


PERSONAS: tuple[Persona, ...] = (
    Persona(
        "dana",
        "Dana Whitfield",
        "dana.whitfield@example.com",
        "Her $349.99 headphones arrived with a cracked headband. The refund needs an operator.",
        (
            PersonaCheck(
                "PV-800101",
                RefundRequest(RefundReason.DAMAGED_OR_DEFECTIVE, (LineRequest(1, 1),)),
                Outcome.NEEDS_APPROVAL,
            ),
        ),
    ),
    Persona(
        "marcus",
        "Marcus Reyes",
        "marcus.reyes@example.com",
        "His laptop order is still processing, so he can change the shipping address.",
        (PersonaCheck("PV-800201", AddressChangeRequest("US"), Outcome.ALLOW),),
    ),
    Persona(
        "priya",
        "Priya Raman",
        "priya.raman@example.com",
        "Her tablet is in transit. Changing the address needs an operator to reroute it.",
        (PersonaCheck("PV-800301", AddressChangeRequest("US"), Outcome.NEEDS_APPROVAL),),
    ),
    Persona(
        "leo",
        "Leo Okafor",
        "leo.okafor@example.com",
        "Two deliveries, 29 and 31 days ago: one can be returned, the other can't.",
        (
            PersonaCheck(
                "PV-800401",
                ReturnRequest((LineRequest(1, 1),), ReturnReason.CHANGED_MIND),
                Outcome.ALLOW,
            ),
            PersonaCheck(
                "PV-800402",
                ReturnRequest((LineRequest(1, 1),), ReturnReason.CHANGED_MIND),
                Outcome.DENY,
            ),
        ),
    ),
    Persona(
        "sam",
        "Sam Castillo",
        "sam.castillo@example.com",
        "No tracking scan for 9 days, so a lost-package refund applies.",
        (PersonaCheck("PV-800501", RefundRequest(RefundReason.LOST_IN_TRANSIT), Outcome.ALLOW),),
    ),
    Persona(
        "alex",
        "Alex Morgan",
        "alex.morgan@example.com",
        "An order arrived 4 days late, so the shipping fee comes back. Alex wants to vent.",
        (PersonaCheck("PV-800601", RefundRequest(RefundReason.LATE_DELIVERY), Outcome.ALLOW),),
    ),
)

PERSONA_EMAILS = frozenset(persona.email for persona in PERSONAS)


@dataclass(frozen=True, slots=True)
class Clone:
    """A visitor's private copy of personas (D4): its own emails and order numbers."""

    visitor_id: str
    numbers: Mapping[str, str]  # persona order number -> the clone's order number

    def email(self, original: str) -> str:
        local, domain = original.split("@")
        return f"{local}+{self.visitor_id}@{domain}"


@dataclass(slots=True)
class PersonaRows:
    customers: list[Customer] = field(default_factory=list)
    orders: list[Order] = field(default_factory=list)


def build_personas(
    ids: Ids,
    now: datetime,
    policy: PolicyConfig,
    *,
    only: Collection[str] | None = None,
    clone: Clone | None = None,
) -> PersonaRows:
    """The personas' customers and orders, all dated relative to `now`.

    `only` limits the build to some persona keys. With a `clone`, emails and order numbers are
    the clone's and the customers carry its visitor ID; everything else is identical.
    """
    tz = policy.tz
    today = local_date(now, tz)
    rates = policy.shipping
    rows = PersonaRows()
    wanted = set(only) if only is not None else {persona.key for persona in PERSONAS}

    def to_clone(original: str) -> str:
        return clone.numbers[original] if clone else original

    def day(days_ago: int, hour: int, minute: int = 0) -> datetime:
        moment = at_local(today - timedelta(days=days_ago), hour, minute, tz)
        return moment if moment < now else moment - timedelta(days=1)

    def person(key: str, address: Address, member_days: int) -> Customer:
        persona = next(p for p in PERSONAS if p.key == key)
        row = builders.customer(
            ids,
            email=clone.email(persona.email) if clone else persona.email,
            full_name=persona.full_name,
            created_at=now - timedelta(days=member_days),
            address=address,
            phone="+1-614-555-0100",
            demo_visitor_id=clone.visitor_id if clone else None,
        )
        rows.customers.append(row)
        return row

    def delivered(
        number: str,
        owner: Customer,
        address: Address,
        lines: list[tuple[str, int]],
        *,
        delivered_days_ago: int,
        transit_days: int = 3,
        shipping_cents: int = 0,
        promise_days: int | None = None,
        late_by: int = 0,
        brand: str = "Visa",
        last4: str = "4242",
    ) -> None:
        number = to_clone(number)
        promise_days = promise_days or rates.standard_promise_days
        delivered_at = day(delivered_days_ago, 14, 20)
        delivered_on = local_date(delivered_at, tz)
        promised = delivered_on - timedelta(days=late_by) if late_by else None
        shipped = delivered_at - timedelta(days=transit_days, hours=5)
        placed = shipped - timedelta(hours=20)
        if promised is None:
            promised = local_date(placed, tz) + timedelta(days=promise_days)
        else:
            placed = at_local(promised - timedelta(days=promise_days), 10, 15, tz)
            shipped = placed + timedelta(hours=20)
        rows.orders.append(
            builders.order(
                ids,
                owner,
                OrderPlan(
                    number=number,
                    placed_at=placed,
                    status=OrderStatus.DELIVERED,
                    lines=lines,
                    shipping_address=address,
                    shipping_cents=shipping_cents,
                    promised_delivery_on=promised,
                    payment_brand=brand,
                    payment_last4=last4,
                    tracking=TrackingPlan(
                        carrier="UPS",
                        tracking_number=f"1Z{number[3:]}PV0000000",
                        status=ShipmentStatus.DELIVERED,
                        shipped_at=shipped,
                        delivered_at=delivered_at,
                        eta=promised,
                        destination=f"{address.city}, {address.state}",
                        hubs=("Allentown, PA", "Columbus, OH", "Indianapolis, IN"),
                    ),
                ),
            )
        )

    if "dana" in wanted:
        # 1. Dana: cracked headphones, $349.99, delivered 8 days ago.
        dana_home = Address(
            recipient_name="Dana Whitfield",
            line1="418 Maple Avenue",
            line2="Apt 3B",
            city="Columbus",
            state="OH",
            postal_code="43215",
        )
        dana = person("dana", dana_home, 900)
        delivered("PV-800101", dana, dana_home, [("AUD-ANC700", 1)], delivered_days_ago=8)
        delivered(
            "PV-800102",
            dana,
            dana_home,
            [("ACC-USBC2M", 2), ("ACC-CHG65", 1)],
            delivered_days_ago=70,
        )

    if "marcus" in wanted:
        # 2. Marcus: a laptop order placed yesterday, still processing.
        marcus_home = Address(
            recipient_name="Marcus Reyes",
            line1="2210 East Cesar Chavez Street",
            city="Austin",
            state="TX",
            postal_code="78702",
        )
        marcus = person("marcus", marcus_home, 400)
        placed = now - timedelta(hours=20)
        rows.orders.append(
            builders.order(
                ids,
                marcus,
                OrderPlan(
                    number=to_clone("PV-800201"),
                    placed_at=placed,
                    status=OrderStatus.PROCESSING,
                    lines=[("CMP-AIR14", 1), ("ACC-SLEEVE14", 1)],
                    shipping_address=marcus_home,
                    shipping_cents=0,
                    promised_delivery_on=local_date(placed, tz) + timedelta(days=6),
                    payment_brand="Mastercard",
                    payment_last4="5100",
                ),
            )
        )
        delivered(
            "PV-800202",
            marcus,
            marcus_home,
            [("CMP-MOUSE", 1)],
            delivered_days_ago=120,
            shipping_cents=rates.standard_cents,
            brand="Mastercard",
            last4="5100",
        )

    if "priya" in wanted:
        # 3. Priya: a tablet in transit, last scanned this morning.
        priya_home = Address(
            recipient_name="Priya Raman",
            line1="1520 Pine Street",
            line2="Unit 804",
            city="Seattle",
            state="WA",
            postal_code="98101",
        )
        priya = person("priya", priya_home, 650)
        shipped = now - timedelta(days=1, hours=12)
        rows.orders.append(
            builders.order(
                ids,
                priya,
                OrderPlan(
                    number=to_clone("PV-800301"),
                    placed_at=shipped - timedelta(hours=18),
                    status=OrderStatus.SHIPPED,
                    lines=[("TAB-SLATE11", 1), ("TAB-PEN2", 1)],
                    shipping_address=priya_home,
                    shipping_cents=0,
                    promised_delivery_on=local_date(shipped, tz) + timedelta(days=4),
                    payment_brand="Amex",
                    payment_last4="1005",
                    tracking=TrackingPlan(
                        carrier="FedEx",
                        tracking_number=f"{to_clone('PV-800301')[3:]}456789",
                        status=ShipmentStatus.IN_TRANSIT,
                        shipped_at=shipped,
                        last_scan_at=now - timedelta(hours=10),
                        eta=local_date(now, tz) + timedelta(days=2),
                        destination="Seattle, WA",
                        hubs=("Memphis, TN", "Denver, CO"),
                    ),
                ),
            )
        )

    if "leo" in wanted:
        # 4. Leo: one delivery 29 days ago (returnable), one 31 days ago (not).
        leo_home = Address(
            recipient_name="Leo Okafor",
            line1="77 Prospect Place",
            city="Brooklyn",
            state="NY",
            postal_code="11217",
        )
        leo = person("leo", leo_home, 1200)
        delivered("PV-800401", leo, leo_home, [("WRB-PULSE4", 1)], delivered_days_ago=29)
        delivered("PV-800402", leo, leo_home, [("AUD-BUDS2", 1)], delivered_days_ago=31)

    if "sam" in wanted:
        # 5. Sam: no carrier scan for 9 days.
        sam_home = Address(
            recipient_name="Sam Castillo",
            line1="3045 North Kedzie Avenue",
            city="Chicago",
            state="IL",
            postal_code="60618",
        )
        sam = person("sam", sam_home, 300)
        last_scan = day(9, 9, 40)
        shipped = last_scan - timedelta(days=2)
        rows.orders.append(
            builders.order(
                ids,
                sam,
                OrderPlan(
                    number=to_clone("PV-800501"),
                    placed_at=shipped - timedelta(hours=16),
                    status=OrderStatus.SHIPPED,
                    lines=[("HOM-PLUG2", 1), ("HOM-BULB4", 1)],
                    shipping_address=sam_home,
                    shipping_cents=0,
                    promised_delivery_on=local_date(shipped, tz) + timedelta(days=5),
                    payment_brand="Visa",
                    payment_last4="0077",
                    tracking=TrackingPlan(
                        carrier="USPS",
                        tracking_number=f"9400{to_clone('PV-800501')[3:]}000000000001",
                        status=ShipmentStatus.IN_TRANSIT,
                        shipped_at=shipped,
                        last_scan_at=last_scan,
                        eta=local_date(shipped, tz) + timedelta(days=4),
                        destination="Chicago, IL",
                        hubs=("Allentown, PA", "Indianapolis, IN"),
                    ),
                ),
            )
        )

    if "alex" in wanted:
        # 6. Alex: express order delivered 3 days ago, 4 days after the promised date.
        alex_home = Address(
            recipient_name="Alex Morgan",
            line1="905 Elm Street",
            city="Denver",
            state="CO",
            postal_code="80206",
        )
        alex = person("alex", alex_home, 210)
        delivered(
            "PV-800601",
            alex,
            alex_home,
            [("GAM-PAD", 1), ("GAM-HEADSET", 1)],
            delivered_days_ago=3,
            shipping_cents=rates.express_cents,
            promise_days=rates.express_promise_days,
            late_by=4,
            brand="Discover",
            last4="6011",
        )

    return rows
