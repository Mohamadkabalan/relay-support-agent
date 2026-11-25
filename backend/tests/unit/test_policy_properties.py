"""Properties of the policy engine that must hold for any order and any sequence of requests."""

from dataclasses import replace
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from hypothesis import given, settings
from hypothesis import strategies as st

from relay.domain.enums import (
    OrderStatus,
    ProductCondition,
    ProductKind,
    RefundReason,
    ReturnReason,
    ShipmentStatus,
)
from relay.domain.errors import DomainError
from relay.domain.policy import (
    Facts,
    LineFacts,
    LineRequest,
    OrderFacts,
    Outcome,
    RefundRequest,
    ReturnRequest,
    ShipmentFacts,
    evaluate,
)
from relay.domain.policy_config import default_policy

NY = ZoneInfo("America/New_York")
NOW = datetime(2026, 9, 15, 15, 0, tzinfo=NY)
POLICY = default_policy()


@st.composite
def lines(draw: st.DrawFn) -> tuple[LineFacts, ...]:
    count = draw(st.integers(1, 4))
    return tuple(
        LineFacts(
            line_no=no,
            sku=f"SKU-{no}",
            name=f"Item {no}",
            unit_price_cents=draw(st.integers(0, 250_000)),
            quantity=draw(st.integers(1, 3)),
            kind=draw(st.sampled_from(ProductKind)),
            condition=draw(st.sampled_from(ProductCondition)),
            final_sale=draw(st.booleans()),
        )
        for no in range(1, count + 1)
    )


@st.composite
def orders(draw: st.DrawFn) -> OrderFacts:
    items = draw(lines())
    shipping = draw(st.sampled_from([0, 599, 1499]))
    state = draw(st.sampled_from(["delivered", "in_transit", "lost"]))
    if state == "delivered":
        delivered_at = NOW - timedelta(
            days=draw(st.integers(0, 40)), hours=draw(st.integers(0, 23))
        )
        late_by = draw(st.integers(-2, 6))
        return OrderFacts(
            number="PV-900100",
            status=OrderStatus.DELIVERED,
            lines=items,
            shipping_cents=shipping,
            delivered_at=delivered_at,
            promised_delivery_on=delivered_at.astimezone(NY).date() - timedelta(days=late_by),
            shipment=ShipmentFacts(ShipmentStatus.DELIVERED, delivered_at),
        )
    status = ShipmentStatus.LOST if state == "lost" else ShipmentStatus.IN_TRANSIT
    last_scan = NOW - timedelta(days=draw(st.integers(0, 20)))
    return OrderFacts(
        number="PV-900100",
        status=OrderStatus.SHIPPED,
        lines=items,
        shipping_cents=shipping,
        shipment=ShipmentFacts(status, last_scan),
    )


requests = st.one_of(
    st.builds(
        lambda no, qty: RefundRequest(RefundReason.DAMAGED_OR_DEFECTIVE, (LineRequest(no, qty),)),
        st.integers(1, 4),
        st.integers(1, 3),
    ),
    st.just(RefundRequest(RefundReason.LOST_IN_TRANSIT)),
    st.just(RefundRequest(RefundReason.LATE_DELIVERY)),
    st.builds(
        lambda no, qty: ReturnRequest((LineRequest(no, qty),), ReturnReason.CHANGED_MIND),
        st.integers(1, 4),
        st.integers(1, 3),
    ),
)


@settings(max_examples=400)
@given(order=orders(), sequence=st.lists(requests, max_size=12))
def test_total_refunded_never_exceeds_total_paid(
    order: OrderFacts, sequence: list[RefundRequest | ReturnRequest]
) -> None:
    paid = sum(line.unit_price_cents * line.quantity for line in order.lines) + order.shipping_cents
    refunded = 0
    for request in sequence:
        try:
            decision = evaluate(request, Facts(order), POLICY, NOW)
        except DomainError:
            continue  # an invalid request (no such line) changes nothing
        if decision.outcome is Outcome.DENY:
            continue
        # Count every allowed outcome as paid out, including returns (refunded on receipt).
        refunded += decision.amount_cents
        order = _apply(order, request)
        assert refunded <= paid


def _apply(order: OrderFacts, request: RefundRequest | ReturnRequest) -> OrderFacts:
    if isinstance(request, ReturnRequest):
        returned = {item.line_no: item.quantity for item in request.lines}
        return replace(
            order,
            lines=tuple(
                replace(line, returned_qty=line.returned_qty + returned.get(line.line_no, 0))
                for line in order.lines
            ),
        )
    match request.reason:
        case RefundReason.DAMAGED_OR_DEFECTIVE:
            refunded = {item.line_no: item.quantity for item in request.lines}
            return replace(
                order,
                lines=tuple(
                    replace(line, refunded_qty=line.refunded_qty + refunded.get(line.line_no, 0))
                    for line in order.lines
                ),
            )
        case RefundReason.LOST_IN_TRANSIT:
            return replace(
                order,
                shipping_refunded=True,
                lines=tuple(
                    replace(line, refunded_qty=line.refunded_qty + line.available_qty)
                    for line in order.lines
                ),
            )
        case _:
            return replace(order, shipping_refunded=True)


@given(order=orders(), request=requests)
def test_decisions_are_deterministic(order: OrderFacts, request: RefundRequest) -> None:
    assert _outcome(order, request) == _outcome(order, request)


def _outcome(order: OrderFacts, request: RefundRequest) -> object:
    try:
        return evaluate(request, Facts(order), POLICY, NOW)
    except DomainError as error:
        return (type(error), str(error))


@given(order=orders(), request=requests)
def test_time_of_day_does_not_change_a_decision(
    order: OrderFacts, request: RefundRequest | ReturnRequest
) -> None:
    """Business rules count store-local calendar days, so 00:05 and 23:55 on the same date agree."""
    day = NOW.date()
    early = datetime.combine(day, time(0, 5), tzinfo=NY)
    late = datetime.combine(day, time(23, 55), tzinfo=NY)
    try:
        at_early = evaluate(request, Facts(order), POLICY, early)
        at_late = evaluate(request, Facts(order), POLICY, late)
    except DomainError:
        return
    assert at_early.outcome == at_late.outcome
    assert at_early.amount_cents == at_late.amount_cents
