"""The policy engine.

`evaluate(request, facts, config, now) -> Decision` is a pure function. The domain services gather
the facts (the order, its lines, the shipment, earlier returns and refunds, the customer's recent
refunds); the engine never reads the clock or the database itself, so every boundary can be tested
with a table of facts.

Rules run in this order (PLAN.md §7):

1. Ownership: enforced before the engine runs, by loading the order through the customer's scope.
2. Input validation: raises `InvalidArgumentError` or `LineNotFoundError`.
3. Denial rules, in table order. Every match is recorded.
4. Approval triggers, only when nothing was denied. Every match is recorded.
5. Allow.
"""

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import StrEnum

from relay.clock import local_date
from relay.domain.enums import (
    OrderStatus,
    ProductCondition,
    ProductKind,
    RefundReason,
    ReturnReason,
    ShipmentStatus,
)
from relay.domain.errors import InvalidArgumentError, LineNotFoundError
from relay.domain.money import format_cents
from relay.domain.policy_config import PolicyConfig


class Outcome(StrEnum):
    ALLOW = "allow"
    NEEDS_APPROVAL = "needs_approval"
    DENY = "deny"


class Effect(StrEnum):
    DENY = "deny"
    NEEDS_APPROVAL = "needs_approval"


@dataclass(frozen=True, slots=True)
class RuleHit:
    rule: str  # "RET-2"
    effect: Effect
    code: str  # "RETURN_WINDOW_EXPIRED"
    explanation: str  # plain language, safe to show the customer
    line_no: int | None = None


# ---------------------------------------------------------------- facts


@dataclass(frozen=True, slots=True)
class LineFacts:
    line_no: int
    sku: str
    name: str
    unit_price_cents: int
    quantity: int
    kind: ProductKind = ProductKind.PHYSICAL
    condition: ProductCondition = ProductCondition.NEW
    final_sale: bool = False
    returned_qty: int = 0  # units in returns that weren't cancelled
    refunded_qty: int = 0  # units refunded without a return (damaged, lost)

    @property
    def available_qty(self) -> int:
        """Units that can still be returned or refunded."""
        return max(0, self.quantity - self.returned_qty - self.refunded_qty)

    @property
    def return_class(self) -> str | None:
        """Why a line might be non-returnable, in the vocabulary of `returns.non_returnable`."""
        if self.final_sale:
            return "final_sale"
        if self.kind is not ProductKind.PHYSICAL:
            return self.kind.value
        return None


@dataclass(frozen=True, slots=True)
class ShipmentFacts:
    status: ShipmentStatus
    last_scan_at: datetime | None


@dataclass(frozen=True, slots=True)
class OrderFacts:
    number: str
    status: OrderStatus
    lines: tuple[LineFacts, ...]
    shipping_cents: int = 0
    shipping_refunded: bool = False
    delivered_at: datetime | None = None
    promised_delivery_on: date | None = None
    shipment: ShipmentFacts | None = None

    def line(self, line_no: int) -> LineFacts | None:
        return next((line for line in self.lines if line.line_no == line_no), None)


@dataclass(frozen=True, slots=True)
class CustomerFacts:
    agent_refunds_issued_at: tuple[datetime, ...] = ()


@dataclass(frozen=True, slots=True)
class Facts:
    order: OrderFacts
    customer: CustomerFacts = field(default_factory=CustomerFacts)


# ---------------------------------------------------------------- requests


@dataclass(frozen=True, slots=True)
class LineRequest:
    line_no: int
    quantity: int


@dataclass(frozen=True, slots=True)
class ReturnRequest:
    lines: tuple[LineRequest, ...]
    reason: ReturnReason


@dataclass(frozen=True, slots=True)
class RefundRequest:
    reason: RefundReason
    lines: tuple[LineRequest, ...] = ()


@dataclass(frozen=True, slots=True)
class AddressChangeRequest:
    country: str


Request = ReturnRequest | RefundRequest | AddressChangeRequest


# ---------------------------------------------------------------- decisions


@dataclass(frozen=True, slots=True)
class Decision:
    outcome: Outcome
    hits: tuple[RuleHit, ...]
    policy_version: str
    amount_cents: int = 0  # refunds: the amount; returns: the refund estimate
    alternatives: tuple[str, ...] = ()

    @property
    def denials(self) -> tuple[RuleHit, ...]:
        return tuple(hit for hit in self.hits if hit.effect is Effect.DENY)

    @property
    def approval_triggers(self) -> tuple[RuleHit, ...]:
        return tuple(hit for hit in self.hits if hit.effect is Effect.NEEDS_APPROVAL)

    @property
    def rules(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(hit.rule for hit in self.hits))

    @property
    def code(self) -> str | None:
        """The code of the first denial, or None."""
        denials = self.denials
        return denials[0].code if denials else None

    @property
    def rule(self) -> str | None:
        denials = self.denials
        return denials[0].rule if denials else None

    @property
    def explanation(self) -> str:
        relevant = self.denials if self.outcome is Outcome.DENY else self.approval_triggers
        return " ".join(dict.fromkeys(hit.explanation for hit in relevant))


def evaluate(request: Request, facts: Facts, config: PolicyConfig, now: datetime) -> Decision:
    match request:
        case ReturnRequest():
            return evaluate_return(request, facts.order, config, now)
        case RefundRequest():
            return evaluate_refund(request, facts, config, now)
        case AddressChangeRequest():
            return evaluate_address_change(request, facts.order, config)


# ---------------------------------------------------------------- returns


def return_window_ends_on(order: OrderFacts, line: LineFacts, config: PolicyConfig) -> date | None:
    """The last day a line can be returned, inclusive, in the store time zone (RET-2)."""
    if order.delivered_at is None:
        return None
    return local_date(order.delivered_at, config.tz) + timedelta(days=_window_days(line, config))


def evaluate_return(
    request: ReturnRequest, order: OrderFacts, config: PolicyConfig, now: datetime
) -> Decision:
    lines = _validate_lines(order, request.lines, required=True)
    hits: list[RuleHit] = []

    if order.status is not OrderStatus.DELIVERED or order.delivered_at is None:
        hits.append(
            RuleHit(
                "RET-1",
                Effect.DENY,
                "NOT_DELIVERED",
                f"Order {order.number} hasn't been delivered yet, so it can't be returned.",
            )
        )
    else:
        today = local_date(now, config.tz)
        delivered_on = local_date(order.delivered_at, config.tz)
        for line, _ in lines:
            ends_on = delivered_on + timedelta(days=_window_days(line, config))
            if today > ends_on:
                hits.append(
                    RuleHit(
                        "RET-2",
                        Effect.DENY,
                        "RETURN_WINDOW_EXPIRED",
                        f"Delivered on {delivered_on.isoformat()}. The "
                        f"{_window_days(line, config)}-day return window for {line.name} ended on "
                        f"{ends_on.isoformat()}.",
                        line.line_no,
                    )
                )
        for line, _ in lines:
            if line.return_class in config.returns.non_returnable:
                hits.append(
                    RuleHit(
                        "RET-3",
                        Effect.DENY,
                        "NOT_RETURNABLE",
                        f"{line.name} is {_describe_return_class(line.return_class)} and can't be "
                        "returned.",
                        line.line_no,
                    )
                )
        for line, quantity in lines:
            if quantity > line.available_qty:
                hits.append(
                    RuleHit(
                        "RET-4",
                        Effect.DENY,
                        "QUANTITY_UNAVAILABLE",
                        f"Only {line.available_qty} of {line.quantity} x {line.name} can still be "
                        "returned; the rest were already returned or refunded.",
                        line.line_no,
                    )
                )

    estimate = sum(line.unit_price_cents * quantity for line, quantity in lines)
    return _decide(hits, config, amount_cents=estimate)


# ---------------------------------------------------------------- refunds


def evaluate_refund(
    request: RefundRequest, facts: Facts, config: PolicyConfig, now: datetime
) -> Decision:
    order = facts.order
    match request.reason:
        case RefundReason.DAMAGED_OR_DEFECTIVE:
            hits, amount = _damaged_refund(request, order, config, now)
        case RefundReason.LOST_IN_TRANSIT:
            _reject_lines(request)
            hits, amount = _lost_refund(order, config, now)
        case RefundReason.LATE_DELIVERY:
            _reject_lines(request)
            hits, amount = _late_refund(order, config)
        case RefundReason.RETURN_RECEIVED:
            raise InvalidArgumentError(
                "Refunds for returned items are issued by the warehouse when the return arrives.",
                "Use create_return instead.",
            )

    if not hits:
        limit = config.refunds.auto_approve_limit_cents
        if amount > limit:
            hits.append(
                RuleHit(
                    "REF-5",
                    Effect.NEEDS_APPROVAL,
                    "OVER_AUTO_APPROVE_LIMIT",
                    f"Refunds over {format_cents(limit)} are reviewed by our team, usually "
                    f"{config.approvals.expected_decision}.",
                )
            )
        recent = count_recent_agent_refunds(facts.customer, config, now)
        if recent >= config.refunds.velocity_refunds_before_review:
            hits.append(
                RuleHit(
                    "REF-6",
                    Effect.NEEDS_APPROVAL,
                    "REFUND_VELOCITY",
                    "This account has had several refunds recently, so our team reviews this "
                    f"one, usually {config.approvals.expected_decision}.",
                )
            )
    return _decide(hits, config, amount_cents=amount)


def count_recent_agent_refunds(customer: CustomerFacts, config: PolicyConfig, now: datetime) -> int:
    """Agent-issued refunds in the trailing velocity window (REF-6)."""
    since = now - timedelta(days=config.refunds.velocity_window_days)
    return sum(1 for issued_at in customer.agent_refunds_issued_at if issued_at >= since)


def days_without_scan(shipment: ShipmentFacts, config: PolicyConfig, now: datetime) -> int | None:
    """Calendar days in the store time zone since the last carrier scan (REF-2)."""
    if shipment.last_scan_at is None:
        return None
    return (local_date(now, config.tz) - local_date(shipment.last_scan_at, config.tz)).days


def is_lost(order: OrderFacts, config: PolicyConfig, now: datetime) -> bool:
    shipment = order.shipment
    if order.status is not OrderStatus.SHIPPED or shipment is None:
        return False
    if shipment.status is ShipmentStatus.LOST:
        return True
    if shipment.status is not ShipmentStatus.IN_TRANSIT:
        return False
    idle = days_without_scan(shipment, config, now)
    return idle is not None and idle >= config.refunds.lost_package_days_without_scan


def days_late(order: OrderFacts, config: PolicyConfig) -> int | None:
    """Days between the promised date and the delivery date, in the store time zone."""
    if order.delivered_at is None or order.promised_delivery_on is None:
        return None
    return (local_date(order.delivered_at, config.tz) - order.promised_delivery_on).days


def _damaged_refund(
    request: RefundRequest, order: OrderFacts, config: PolicyConfig, now: datetime
) -> tuple[list[RuleHit], int]:
    lines = _validate_lines(order, request.lines, required=True)
    hits: list[RuleHit] = []
    if order.status is not OrderStatus.DELIVERED or order.delivered_at is None:
        hits.append(
            RuleHit(
                "REF-1",
                Effect.DENY,
                "NOT_DELIVERED",
                f"Order {order.number} hasn't been delivered yet. A damaged-item refund applies "
                "once it arrives.",
            )
        )
    else:
        today = local_date(now, config.tz)
        delivered_on = local_date(order.delivered_at, config.tz)
        for line, _ in lines:
            if line.kind is not ProductKind.PHYSICAL:
                hits.append(
                    RuleHit(
                        "REF-1",
                        Effect.DENY,
                        "NOT_PHYSICAL_ITEM",
                        f"{line.name} is {_describe_return_class(line.kind.value)}, so it can't "
                        "be refunded as damaged.",
                        line.line_no,
                    )
                )
        for line, _ in lines:
            ends_on = delivered_on + timedelta(days=_window_days(line, config))
            if today > ends_on:
                hits.append(
                    RuleHit(
                        "REF-1",
                        Effect.DENY,
                        "OUTSIDE_WINDOW",
                        f"Delivered on {delivered_on.isoformat()}. Damage has to be reported "
                        f"within {_window_days(line, config)} days for {line.name}, by "
                        f"{ends_on.isoformat()}.",
                        line.line_no,
                    )
                )
    for line, quantity in lines:
        if quantity > line.available_qty:
            hits.append(
                RuleHit(
                    "REF-4",
                    Effect.DENY,
                    "ALREADY_REFUNDED",
                    f"Only {line.available_qty} of {line.quantity} x {line.name} can still be "
                    "refunded; the rest were already refunded or are part of a return.",
                    line.line_no,
                )
            )
    amount = sum(line.unit_price_cents * quantity for line, quantity in lines)
    return hits, (0 if hits else amount)


def _lost_refund(
    order: OrderFacts, config: PolicyConfig, now: datetime
) -> tuple[list[RuleHit], int]:
    if not is_lost(order, config, now):
        return [
            RuleHit(
                "REF-2",
                Effect.DENY,
                "NOT_LOST",
                _not_lost_explanation(order, config, now),
            )
        ], 0
    amount = sum(line.unit_price_cents * line.available_qty for line in order.lines)
    if not order.shipping_refunded:
        amount += order.shipping_cents
    if amount == 0:
        return [
            RuleHit(
                "REF-4",
                Effect.DENY,
                "ALREADY_REFUNDED",
                f"Order {order.number} has already been fully refunded.",
            )
        ], 0
    return [], amount


def _late_refund(order: OrderFacts, config: PolicyConfig) -> tuple[list[RuleHit], int]:
    grace = config.refunds.late_delivery_grace_days
    late_by = days_late(order, config)
    if order.status is not OrderStatus.DELIVERED or late_by is None:
        explanation = (
            f"Order {order.number} hasn't been delivered yet. A late-delivery refund is "
            "possible once it arrives."
        )
        return [RuleHit("REF-3", Effect.DENY, "NOT_LATE", explanation)], 0
    if late_by < grace:
        explanation = (
            f"Order {order.number} arrived {_days_late_text(late_by)}. Shipping is refunded "
            f"when an order arrives {grace} or more days after the promised date."
        )
        return [RuleHit("REF-3", Effect.DENY, "NOT_LATE", explanation)], 0
    if order.shipping_refunded:
        explanation = f"The shipping fee on order {order.number} was already refunded."
        return [RuleHit("REF-3", Effect.DENY, "ALREADY_REFUNDED", explanation)], 0
    if order.shipping_cents == 0:
        explanation = f"Order {order.number} shipped for free, so there is no fee to refund."
        return [RuleHit("REF-3", Effect.DENY, "NO_SHIPPING_FEE", explanation)], 0
    return [], order.shipping_cents


# ---------------------------------------------------------------- address changes


def evaluate_address_change(
    request: AddressChangeRequest, order: OrderFacts, config: PolicyConfig
) -> Decision:
    rules = config.address_changes
    denials: list[RuleHit] = []
    approvals: list[RuleHit] = []
    shipment = order.shipment

    if order.status in rules.allowed_order_statuses:
        pass  # ADR-1: not shipped yet, change it directly
    elif (
        order.status is OrderStatus.SHIPPED
        and shipment is not None
        and shipment.status in rules.approval_carrier_statuses
    ):
        approvals.append(
            RuleHit(
                "ADR-2",
                Effect.NEEDS_APPROVAL,
                "CARRIER_REROUTE",
                f"Order {order.number} has already shipped, so our team has to ask the carrier "
                f"to reroute it, usually {config.approvals.expected_decision}.",
            )
        )
    else:
        denials.append(
            RuleHit("ADR-3", Effect.DENY, "TOO_LATE_TO_CHANGE", _too_late_explanation(order))
        )

    if request.country.upper() not in rules.supported_countries:
        denials.append(
            RuleHit(
                "ADR-4",
                Effect.DENY,
                "INTERNATIONAL_NOT_SUPPORTED",
                "We only ship to addresses in the United States.",
            )
        )
    return _decide(denials or approvals, config)


# ---------------------------------------------------------------- read-only view


@dataclass(frozen=True, slots=True)
class Availability:
    allowed: bool
    needs_approval: bool = False
    code: str | None = None  # why not, when not allowed
    window_ends_on: date | None = None
    lines: tuple[int, ...] = ()  # line numbers the action applies to


@dataclass(frozen=True, slots=True)
class AllowedActions:
    """What the policy allows on an order right now. `get_order` shows it to the model."""

    return_: Availability
    refund: dict[RefundReason, Availability]
    change_address: Availability


def allowed_actions(facts: Facts, config: PolicyConfig, now: datetime) -> AllowedActions:
    order = facts.order
    return AllowedActions(
        return_=_per_line(
            order,
            config,
            lambda line: evaluate_return(
                ReturnRequest((LineRequest(line.line_no, 1),), ReturnReason.CHANGED_MIND),
                order,
                config,
                now,
            ),
        ),
        refund={
            RefundReason.DAMAGED_OR_DEFECTIVE: _per_line(
                order,
                config,
                lambda line: evaluate_refund(
                    RefundRequest(
                        RefundReason.DAMAGED_OR_DEFECTIVE, (LineRequest(line.line_no, 1),)
                    ),
                    facts,
                    config,
                    now,
                ),
            ),
            RefundReason.LOST_IN_TRANSIT: _availability(
                evaluate_refund(RefundRequest(RefundReason.LOST_IN_TRANSIT), facts, config, now)
            ),
            RefundReason.LATE_DELIVERY: _availability(
                evaluate_refund(RefundRequest(RefundReason.LATE_DELIVERY), facts, config, now)
            ),
        },
        change_address=_availability(
            evaluate_address_change(AddressChangeRequest("US"), order, config)
        ),
    )


def _per_line(
    order: OrderFacts,
    config: PolicyConfig,
    decide: Callable[[LineFacts], Decision],
) -> Availability:
    allowed: list[LineFacts] = []
    first_code: str | None = None
    for line in order.lines:
        decision = decide(line)
        if decision.outcome is Outcome.DENY:
            first_code = first_code or decision.code
        else:
            allowed.append(line)
    if not allowed:
        return Availability(allowed=False, code=first_code or "NOTHING_ELIGIBLE")
    ends = [d for line in allowed if (d := return_window_ends_on(order, line, config))]
    return Availability(
        allowed=True,
        window_ends_on=min(ends) if ends else None,
        lines=tuple(line.line_no for line in allowed),
    )


def _availability(decision: Decision) -> Availability:
    return Availability(
        allowed=decision.outcome is not Outcome.DENY,
        needs_approval=decision.outcome is Outcome.NEEDS_APPROVAL,
        code=decision.code,
    )


# ---------------------------------------------------------------- helpers


def _validate_lines(
    order: OrderFacts, requested: Iterable[LineRequest], *, required: bool
) -> list[tuple[LineFacts, int]]:
    requested = tuple(requested)
    if required and not requested:
        raise InvalidArgumentError(
            "Say which order lines this is for.", "Pass lines with line numbers from get_order."
        )
    seen: set[int] = set()
    resolved: list[tuple[LineFacts, int]] = []
    for item in requested:
        if item.line_no in seen:
            raise InvalidArgumentError(
                f"Line {item.line_no} is listed twice.", "List each line once with its quantity."
            )
        seen.add(item.line_no)
        if item.quantity < 1:
            raise InvalidArgumentError(
                f"Quantity for line {item.line_no} must be at least 1.",
                "Use the number of units the customer means.",
            )
        line = order.line(item.line_no)
        if line is None:
            raise LineNotFoundError(order.number, item.line_no)
        resolved.append((line, item.quantity))
    return resolved


def _reject_lines(request: RefundRequest) -> None:
    if request.lines:
        raise InvalidArgumentError(
            f"Lines only apply to damaged_or_defective refunds, not {request.reason.value}.",
            "Call it again without lines.",
        )


def _window_days(line: LineFacts, config: PolicyConfig) -> int:
    if line.condition is ProductCondition.REFURBISHED:
        return config.returns.window_days_refurbished
    return config.returns.window_days


def _describe_return_class(return_class: str | None) -> str:
    return {
        "final_sale": "a final-sale item",
        "gift_card": "a gift card",
        "digital": "a digital product",
    }.get(return_class or "", "not returnable")


def _days_late_text(late_by: int) -> str:
    if late_by <= 0:
        return "on time"
    return "1 day late" if late_by == 1 else f"{late_by} days late"


def _not_lost_explanation(order: OrderFacts, config: PolicyConfig, now: datetime) -> str:
    threshold = config.refunds.lost_package_days_without_scan
    shipment = order.shipment
    if order.status is OrderStatus.DELIVERED:
        return f"Order {order.number} was delivered, so it isn't lost."
    if order.status is not OrderStatus.SHIPPED or shipment is None:
        return f"Order {order.number} hasn't shipped yet, so it can't be lost in transit."
    idle = days_without_scan(shipment, config, now)
    if shipment.status is ShipmentStatus.IN_TRANSIT and idle is not None:
        return (
            f"The last tracking scan for order {order.number} was {idle} day"
            f"{'' if idle == 1 else 's'} ago. A package counts as lost after {threshold} days "
            "without a scan."
        )
    return f"Order {order.number} is still moving through the carrier's network."


def _too_late_explanation(order: OrderFacts) -> str:
    if order.status is OrderStatus.DELIVERED:
        return f"Order {order.number} has already been delivered."
    if order.status is OrderStatus.CANCELLED:
        return f"Order {order.number} was cancelled."
    shipment_status = order.shipment.status if order.shipment else None
    if shipment_status is ShipmentStatus.OUT_FOR_DELIVERY:
        return f"Order {order.number} is already out for delivery."
    if shipment_status is ShipmentStatus.LOST:
        return f"The carrier has reported order {order.number} as lost."
    return f"Order {order.number} can't be redirected at this point."


_ALTERNATIVES: dict[str, tuple[str, ...]] = {
    "RETURN_WINDOW_EXPIRED": (
        "If the item is faulty, the manufacturer warranty may cover it (help article: "
        "warranty-claims).",
    ),
    "OUTSIDE_WINDOW": (
        "The manufacturer warranty may cover a defect (help article: warranty-claims).",
    ),
    "NOT_RETURNABLE": (
        "If the item arrived damaged or doesn't work, a damaged-item refund may still apply.",
    ),
    "NOT_DELIVERED": ("Track the shipment to see when it will arrive.",),
    "NOT_PHYSICAL_ITEM": ("If a code or gift card doesn't work, our team can look into it.",),
    "NOT_LOST": ("Track the shipment for the latest carrier update.",),
    "TOO_LATE_TO_CHANGE": ("Our team can contact the carrier if the customer asks for a human.",),
}


def _decide(hits: list[RuleHit], config: PolicyConfig, *, amount_cents: int = 0) -> Decision:
    if any(hit.effect is Effect.DENY for hit in hits):
        codes = dict.fromkeys(hit.code for hit in hits if hit.effect is Effect.DENY)
        alternatives = tuple(
            dict.fromkeys(alt for code in codes for alt in _ALTERNATIVES.get(code, ()))
        )
        return Decision(
            Outcome.DENY, tuple(hits), config.version, amount_cents=0, alternatives=alternatives
        )
    outcome = Outcome.NEEDS_APPROVAL if hits else Outcome.ALLOW
    return Decision(outcome, tuple(hits), config.version, amount_cents=amount_cents)
