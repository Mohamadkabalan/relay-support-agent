"""The policy engine, boundary by boundary (PLAN.md §7).

Every test builds facts by hand and evaluates them at a fixed moment: 2026-09-15 15:00 in New York,
unless the test is about the clock itself.
"""

from dataclasses import replace
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from relay.domain.enums import (
    OrderStatus,
    ProductCondition,
    ProductKind,
    RefundReason,
    ReturnReason,
    ShipmentStatus,
)
from relay.domain.errors import InvalidArgumentError, LineNotFoundError
from relay.domain.policy import (
    AddressChangeRequest,
    CustomerFacts,
    Effect,
    Facts,
    LineFacts,
    LineRequest,
    OrderFacts,
    Outcome,
    RefundRequest,
    ReturnRequest,
    ShipmentFacts,
    allowed_actions,
    evaluate,
    return_window_ends_on,
)
from relay.domain.policy_config import PolicyConfig, default_policy

NY = ZoneInfo("America/New_York")
NOW = datetime(2026, 9, 15, 15, 0, tzinfo=NY)
POLICY = default_policy()


def at(day: date, hour: int = 14, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=NY)


def line(
    no: int = 1,
    price: int = 34999,
    qty: int = 1,
    *,
    kind: ProductKind = ProductKind.PHYSICAL,
    condition: ProductCondition = ProductCondition.NEW,
    final_sale: bool = False,
    returned: int = 0,
    refunded: int = 0,
) -> LineFacts:
    return LineFacts(
        line_no=no,
        sku=f"SKU-{no}",
        name=f"Item {no}",
        unit_price_cents=price,
        quantity=qty,
        kind=kind,
        condition=condition,
        final_sale=final_sale,
        returned_qty=returned,
        refunded_qty=refunded,
    )


def delivered(
    days_ago: int = 5,
    *,
    lines: tuple[LineFacts, ...] = (line(),),
    hour: int = 14,
    minute: int = 0,
    shipping: int = 0,
    late_by: int | None = None,
    shipping_refunded: bool = False,
) -> OrderFacts:
    delivered_at = at(NOW.date() - timedelta(days=days_ago), hour, minute)
    promised = None
    if late_by is not None:
        promised = delivered_at.date() - timedelta(days=late_by)
    return OrderFacts(
        number="PV-900001",
        status=OrderStatus.DELIVERED,
        lines=lines,
        shipping_cents=shipping,
        shipping_refunded=shipping_refunded,
        delivered_at=delivered_at,
        promised_delivery_on=promised,
        shipment=ShipmentFacts(ShipmentStatus.DELIVERED, delivered_at),
    )


def shipped(
    shipment_status: ShipmentStatus = ShipmentStatus.IN_TRANSIT,
    *,
    idle_days: int = 1,
    lines: tuple[LineFacts, ...] = (line(),),
    shipping: int = 0,
    shipping_refunded: bool = False,
) -> OrderFacts:
    return OrderFacts(
        number="PV-900002",
        status=OrderStatus.SHIPPED,
        lines=lines,
        shipping_cents=shipping,
        shipping_refunded=shipping_refunded,
        shipment=ShipmentFacts(shipment_status, at(NOW.date() - timedelta(days=idle_days), 9)),
    )


def not_shipped(status: OrderStatus) -> OrderFacts:
    return OrderFacts(number="PV-900003", status=status, lines=(line(),))


def return_one(line_no: int = 1, qty: int = 1) -> ReturnRequest:
    return ReturnRequest((LineRequest(line_no, qty),), ReturnReason.CHANGED_MIND)


def damaged(line_no: int = 1, qty: int = 1) -> RefundRequest:
    return RefundRequest(RefundReason.DAMAGED_OR_DEFECTIVE, (LineRequest(line_no, qty),))


LOST = RefundRequest(RefundReason.LOST_IN_TRANSIT)
LATE = RefundRequest(RefundReason.LATE_DELIVERY)


def decide(
    request: ReturnRequest | RefundRequest | AddressChangeRequest,
    order: OrderFacts,
    *,
    now: datetime = NOW,
    refunds: tuple[datetime, ...] = (),
    policy: PolicyConfig = POLICY,
) -> tuple[Outcome, tuple[str, ...], str | None]:
    decision = evaluate(request, Facts(order, CustomerFacts(refunds)), policy, now)
    return decision.outcome, decision.rules, decision.code


# ---------------------------------------------------------------- returns: the window (RET-2)


@pytest.mark.parametrize(
    ("days_ago", "outcome"),
    [(0, Outcome.ALLOW), (29, Outcome.ALLOW), (30, Outcome.ALLOW), (31, Outcome.DENY)],
)
def test_return_window_is_30_calendar_days_inclusive(days_ago: int, outcome: Outcome) -> None:
    assert decide(return_one(), delivered(days_ago))[0] is outcome


def test_day_31_denial_explains_the_dates() -> None:
    decision = evaluate(return_one(), Facts(delivered(31)), POLICY, NOW)
    assert decision.code == "RETURN_WINDOW_EXPIRED"
    assert decision.rule == "RET-2"
    assert "Delivered on 2026-08-15" in decision.explanation
    assert "ended on 2026-09-14" in decision.explanation
    assert any("warranty" in alternative for alternative in decision.alternatives)


def test_late_evening_delivery_counts_on_its_local_date() -> None:
    # 23:30 in New York on Aug 15 is already Aug 16 in UTC. Counted in UTC, the window would end
    # on Sep 15 (today) and the return would wrongly be allowed.
    order = delivered(31, hour=23, minute=30)
    assert decide(return_one(), order)[0] is Outcome.DENY


def test_late_evening_now_still_counts_as_today() -> None:
    # At 23:30 on Sep 15 in New York it is Sep 16 in UTC; day 30 is still allowed.
    late_now = datetime(2026, 9, 15, 23, 30, tzinfo=NY)
    assert decide(return_one(), delivered(30), now=late_now)[0] is Outcome.ALLOW


@pytest.mark.parametrize(
    ("days_ago", "outcome"),
    [(15, Outcome.ALLOW), (16, Outcome.DENY)],
)
def test_refurbished_items_have_a_15_day_window(days_ago: int, outcome: Outcome) -> None:
    order = delivered(days_ago, lines=(line(condition=ProductCondition.REFURBISHED),))
    assert decide(return_one(), order)[0] is outcome


def test_window_end_date_is_reported_per_line() -> None:
    order = delivered(10, lines=(line(1), line(2, condition=ProductCondition.REFURBISHED)))
    assert return_window_ends_on(order, order.lines[0], POLICY) == date(2026, 10, 5)
    assert return_window_ends_on(order, order.lines[1], POLICY) == date(2026, 9, 20)


# ---------------------------------------------------------------- returns: other rules


@pytest.mark.parametrize(
    "status",
    [OrderStatus.PENDING, OrderStatus.PROCESSING, OrderStatus.CANCELLED],
)
def test_orders_that_were_never_delivered_cannot_be_returned(status: OrderStatus) -> None:
    assert decide(return_one(), not_shipped(status)) == (Outcome.DENY, ("RET-1",), "NOT_DELIVERED")


def test_shipped_orders_cannot_be_returned_yet() -> None:
    assert decide(return_one(), shipped())[2] == "NOT_DELIVERED"


@pytest.mark.parametrize(
    ("item", "code"),
    [
        (line(final_sale=True), "NOT_RETURNABLE"),
        (line(kind=ProductKind.GIFT_CARD), "NOT_RETURNABLE"),
        (line(kind=ProductKind.DIGITAL), "NOT_RETURNABLE"),
    ],
)
def test_non_returnable_items(item: LineFacts, code: str) -> None:
    assert decide(return_one(), delivered(lines=(item,))) == (Outcome.DENY, ("RET-3",), code)


@pytest.mark.parametrize(
    ("returned", "refunded", "qty", "outcome"),
    [
        (0, 0, 2, Outcome.ALLOW),
        (1, 0, 1, Outcome.ALLOW),
        (1, 0, 2, Outcome.DENY),
        (0, 1, 2, Outcome.DENY),
        (1, 1, 1, Outcome.DENY),
    ],
)
def test_return_quantity_counts_earlier_returns_and_refunds(
    returned: int, refunded: int, qty: int, outcome: Outcome
) -> None:
    order = delivered(lines=(line(qty=2, returned=returned, refunded=refunded),))
    result = decide(return_one(qty=qty), order)
    assert result[0] is outcome
    if outcome is Outcome.DENY:
        assert result[1:] == (("RET-4",), "QUANTITY_UNAVAILABLE")


def test_every_matching_denial_is_recorded() -> None:
    order = delivered(40, lines=(line(final_sale=True, qty=1, returned=1),))
    decision = evaluate(return_one(), Facts(order), POLICY, NOW)
    assert decision.rules == ("RET-2", "RET-3", "RET-4")
    assert decision.code == "RETURN_WINDOW_EXPIRED"  # the first rule in table order


def test_a_mixed_order_denies_only_if_a_requested_line_is_ineligible() -> None:
    order = delivered(lines=(line(1), line(2, final_sale=True)))
    assert decide(return_one(1), order)[0] is Outcome.ALLOW
    request = ReturnRequest((LineRequest(1, 1), LineRequest(2, 1)), ReturnReason.CHANGED_MIND)
    assert decide(request, order)[2] == "NOT_RETURNABLE"


def test_return_estimate_is_the_price_of_the_returned_units() -> None:
    order = delivered(lines=(line(1, price=1999, qty=3), line(2, price=500)))
    request = ReturnRequest((LineRequest(1, 2), LineRequest(2, 1)), ReturnReason.NO_LONGER_NEEDED)
    assert evaluate(request, Facts(order), POLICY, NOW).amount_cents == 2 * 1999 + 500


def test_returns_never_need_approval() -> None:
    order = delivered(lines=(line(price=250000),))
    assert decide(return_one(), order, refunds=(NOW, NOW, NOW))[0] is Outcome.ALLOW


# ---------------------------------------------------------------- input validation


@pytest.mark.parametrize(
    ("request_", "error"),
    [
        (ReturnRequest((), ReturnReason.CHANGED_MIND), InvalidArgumentError),
        (ReturnRequest((LineRequest(1, 0),), ReturnReason.CHANGED_MIND), InvalidArgumentError),
        (
            ReturnRequest((LineRequest(1, 1), LineRequest(1, 1)), ReturnReason.CHANGED_MIND),
            InvalidArgumentError,
        ),
        (ReturnRequest((LineRequest(7, 1),), ReturnReason.CHANGED_MIND), LineNotFoundError),
        (RefundRequest(RefundReason.DAMAGED_OR_DEFECTIVE, ()), InvalidArgumentError),
        (
            RefundRequest(RefundReason.LOST_IN_TRANSIT, (LineRequest(1, 1),)),
            InvalidArgumentError,
        ),
        (RefundRequest(RefundReason.LATE_DELIVERY, (LineRequest(1, 1),)), InvalidArgumentError),
        (RefundRequest(RefundReason.RETURN_RECEIVED), InvalidArgumentError),
    ],
)
def test_invalid_requests_raise_before_any_rule(
    request_: ReturnRequest | RefundRequest, error: type[Exception]
) -> None:
    with pytest.raises(error):
        evaluate(request_, Facts(delivered()), POLICY, NOW)


def test_line_not_found_names_the_order_and_line() -> None:
    with pytest.raises(
        LineNotFoundError, match=r"\[LINE_NOT_FOUND\] Order PV-900001 has no line 7"
    ):
        evaluate(return_one(7), Facts(delivered()), POLICY, NOW)


# ---------------------------------------------------------------- refunds: damaged (REF-1, REF-4)


@pytest.mark.parametrize(
    ("days_ago", "outcome", "code"),
    [(30, Outcome.NEEDS_APPROVAL, None), (31, Outcome.DENY, "OUTSIDE_WINDOW")],
)
def test_damage_must_be_reported_inside_the_return_window(
    days_ago: int, outcome: Outcome, code: str | None
) -> None:
    result = decide(damaged(), delivered(days_ago))
    assert (result[0], result[2]) == (outcome, code)


def test_damage_refund_needs_a_delivered_order() -> None:
    assert decide(damaged(), shipped())[1:] == (("REF-1",), "NOT_DELIVERED")


def test_final_sale_items_can_still_be_refunded_as_damaged() -> None:
    order = delivered(lines=(line(price=5000, final_sale=True),))
    assert decide(damaged(), order)[0] is Outcome.ALLOW


@pytest.mark.parametrize("kind", [ProductKind.GIFT_CARD, ProductKind.DIGITAL])
def test_gift_cards_and_digital_items_cannot_be_refunded_as_damaged(kind: ProductKind) -> None:
    order = delivered(lines=(line(price=2500, kind=kind),))
    assert decide(damaged(), order)[1:] == (("REF-1",), "NOT_PHYSICAL_ITEM")


def test_damage_refund_cannot_cover_units_already_refunded_or_returned() -> None:
    order = delivered(lines=(line(price=5000, qty=2, refunded=1, returned=1),))
    assert decide(damaged(), order)[1:] == (("REF-4",), "ALREADY_REFUNDED")


def test_damage_refund_amount_is_unit_price_times_quantity() -> None:
    order = delivered(lines=(line(price=2999, qty=3),))
    decision = evaluate(damaged(qty=2), Facts(order), POLICY, NOW)
    assert (decision.outcome, decision.amount_cents) == (Outcome.ALLOW, 5998)


# ---------------------------------------------------------------- refunds: approval triggers


@pytest.mark.parametrize(
    ("price", "outcome"),
    [(9999, Outcome.ALLOW), (10000, Outcome.ALLOW), (10001, Outcome.NEEDS_APPROVAL)],
)
def test_refunds_over_100_dollars_need_approval(price: int, outcome: Outcome) -> None:
    result = decide(damaged(), delivered(lines=(line(price=price),)))
    assert result[0] is outcome
    if outcome is Outcome.NEEDS_APPROVAL:
        assert result[1] == ("REF-5",)


def test_approval_reason_is_customer_facing() -> None:
    decision = evaluate(damaged(), Facts(delivered()), POLICY, NOW)
    assert decision.approval_triggers[0].code == "OVER_AUTO_APPROVE_LIMIT"
    assert "Refunds over $100.00 are reviewed" in decision.explanation


@pytest.mark.parametrize(
    ("recent", "outcome"),
    [
        (0, Outcome.ALLOW),
        (1, Outcome.ALLOW),
        (2, Outcome.NEEDS_APPROVAL),
        (3, Outcome.NEEDS_APPROVAL),
    ],
)
def test_velocity_rule_counts_agent_refunds(recent: int, outcome: Outcome) -> None:
    refunds = tuple(NOW - timedelta(days=10 * (i + 1)) for i in range(recent))
    result = decide(damaged(), delivered(lines=(line(price=2000),)), refunds=refunds)
    assert result[0] is outcome
    if outcome is Outcome.NEEDS_APPROVAL:
        assert result[1] == ("REF-6",)


@pytest.mark.parametrize(
    ("age", "counted"),
    [(timedelta(days=90), True), (timedelta(days=90, seconds=1), False)],
)
def test_velocity_window_is_90_days(age: timedelta, counted: bool) -> None:
    refunds = (NOW - timedelta(days=1), NOW - age)
    outcome = decide(damaged(), delivered(lines=(line(price=2000),)), refunds=refunds)[0]
    assert (outcome is Outcome.NEEDS_APPROVAL) is counted


def test_both_approval_triggers_are_recorded() -> None:
    refunds = (NOW - timedelta(days=1), NOW - timedelta(days=2))
    assert decide(damaged(), delivered(), refunds=refunds)[1] == ("REF-5", "REF-6")


def test_a_denial_skips_the_approval_triggers() -> None:
    refunds = (NOW - timedelta(days=1), NOW - timedelta(days=2))
    decision = evaluate(damaged(), Facts(delivered(40), CustomerFacts(refunds)), POLICY, NOW)
    assert decision.rules == ("REF-1",)
    assert decision.amount_cents == 0


# ---------------------------------------------------------------- refunds: lost (REF-2)


@pytest.mark.parametrize(
    ("idle_days", "outcome"),
    [(6, Outcome.DENY), (7, Outcome.ALLOW), (20, Outcome.ALLOW)],
)
def test_in_transit_package_is_lost_after_7_days_without_a_scan(
    idle_days: int, outcome: Outcome
) -> None:
    order = shipped(idle_days=idle_days, lines=(line(price=5000),))
    assert decide(LOST, order)[0] is outcome


def test_not_lost_explains_how_long_until_it_is() -> None:
    decision = evaluate(LOST, Facts(shipped(idle_days=6)), POLICY, NOW)
    assert decision.code == "NOT_LOST"
    assert "6 days ago" in decision.explanation
    assert "after 7 days" in decision.explanation


@pytest.mark.parametrize(
    ("order", "outcome"),
    [
        (shipped(ShipmentStatus.LOST, idle_days=1, lines=(line(price=5000),)), Outcome.ALLOW),
        (shipped(ShipmentStatus.LABEL_CREATED, idle_days=20), Outcome.DENY),
        (shipped(ShipmentStatus.OUT_FOR_DELIVERY, idle_days=20), Outcome.DENY),
        (delivered(), Outcome.DENY),
        (not_shipped(OrderStatus.PROCESSING), Outcome.DENY),
        (not_shipped(OrderStatus.CANCELLED), Outcome.DENY),
    ],
)
def test_lost_refund_by_order_state(order: OrderFacts, outcome: Outcome) -> None:
    assert decide(LOST, order)[0] is outcome


def test_lost_refund_covers_remaining_units_and_shipping() -> None:
    order = shipped(
        idle_days=8,
        lines=(line(1, price=4000, qty=2, refunded=1), line(2, price=1000)),
        shipping=599,
    )
    assert evaluate(LOST, Facts(order), POLICY, NOW).amount_cents == 4000 + 1000 + 599


def test_lost_refund_is_denied_once_everything_was_refunded() -> None:
    order = shipped(idle_days=8, lines=(line(refunded=1),), shipping=599, shipping_refunded=True)
    assert decide(LOST, order)[1:] == (("REF-4",), "ALREADY_REFUNDED")


# ---------------------------------------------------------------- refunds: late (REF-3)


@pytest.mark.parametrize(
    ("late_by", "outcome"),
    [
        (-1, Outcome.DENY),
        (0, Outcome.DENY),
        (1, Outcome.DENY),
        (2, Outcome.ALLOW),
        (6, Outcome.ALLOW),
    ],
)
def test_late_delivery_needs_two_days_past_the_promise(late_by: int, outcome: Outcome) -> None:
    assert decide(LATE, delivered(late_by=late_by, shipping=1499))[0] is outcome


def test_late_refund_is_the_shipping_fee() -> None:
    decision = evaluate(LATE, Facts(delivered(late_by=4, shipping=1499)), POLICY, NOW)
    assert (decision.outcome, decision.amount_cents) == (Outcome.ALLOW, 1499)


@pytest.mark.parametrize(
    ("order", "code"),
    [
        (delivered(late_by=4, shipping=1499, shipping_refunded=True), "ALREADY_REFUNDED"),
        (delivered(late_by=4, shipping=0), "NO_SHIPPING_FEE"),
        (delivered(late_by=None, shipping=1499), "NOT_LATE"),
        (shipped(), "NOT_LATE"),
    ],
)
def test_late_refund_denials(order: OrderFacts, code: str) -> None:
    assert decide(LATE, order)[1:] == (("REF-3",), code)


def test_late_explanation_says_how_late() -> None:
    decision = evaluate(LATE, Facts(delivered(late_by=1, shipping=599)), POLICY, NOW)
    assert "arrived 1 day late" in decision.explanation


# ---------------------------------------------------------------- address changes (ADR-1..4)

US = AddressChangeRequest("US")


@pytest.mark.parametrize(
    ("order", "outcome", "rules"),
    [
        (not_shipped(OrderStatus.PENDING), Outcome.ALLOW, ()),
        (not_shipped(OrderStatus.PROCESSING), Outcome.ALLOW, ()),
        (shipped(ShipmentStatus.LABEL_CREATED), Outcome.NEEDS_APPROVAL, ("ADR-2",)),
        (shipped(ShipmentStatus.IN_TRANSIT), Outcome.NEEDS_APPROVAL, ("ADR-2",)),
        (shipped(ShipmentStatus.OUT_FOR_DELIVERY), Outcome.DENY, ("ADR-3",)),
        (shipped(ShipmentStatus.LOST), Outcome.DENY, ("ADR-3",)),
        (delivered(), Outcome.DENY, ("ADR-3",)),
        (not_shipped(OrderStatus.CANCELLED), Outcome.DENY, ("ADR-3",)),
    ],
)
def test_address_change_by_order_state(
    order: OrderFacts, outcome: Outcome, rules: tuple[str, ...]
) -> None:
    assert decide(US, order)[:2] == (outcome, rules)


@pytest.mark.parametrize("country", ["CA", "GB", "mx"])
def test_address_changes_are_us_only(country: str) -> None:
    assert decide(AddressChangeRequest(country), not_shipped(OrderStatus.PROCESSING))[1:] == (
        ("ADR-4",),
        "INTERNATIONAL_NOT_SUPPORTED",
    )


def test_too_late_and_international_are_both_recorded() -> None:
    assert decide(AddressChangeRequest("CA"), delivered())[1] == ("ADR-3", "ADR-4")


@pytest.mark.parametrize(
    ("order", "fragment"),
    [
        (delivered(), "already been delivered"),
        (not_shipped(OrderStatus.CANCELLED), "was cancelled"),
        (shipped(ShipmentStatus.OUT_FOR_DELIVERY), "out for delivery"),
        (shipped(ShipmentStatus.LOST), "reported order PV-900002 as lost"),
    ],
)
def test_too_late_explanations(order: OrderFacts, fragment: str) -> None:
    assert fragment in evaluate(US, Facts(order), POLICY, NOW).explanation


# ---------------------------------------------------------------- allowed_actions


def test_allowed_actions_for_a_fresh_delivery() -> None:
    order = delivered(
        5,
        lines=(line(1, price=5000), line(2, price=2500, kind=ProductKind.GIFT_CARD)),
        shipping=599,
    )
    actions = allowed_actions(Facts(order), POLICY, NOW)
    assert actions.return_.allowed
    assert actions.return_.lines == (1,)
    assert actions.return_.window_ends_on == date(2026, 10, 10)
    assert actions.refund[RefundReason.DAMAGED_OR_DEFECTIVE].lines == (1,)
    assert not actions.refund[RefundReason.DAMAGED_OR_DEFECTIVE].needs_approval
    assert actions.refund[RefundReason.LOST_IN_TRANSIT].code == "NOT_LOST"
    assert actions.refund[RefundReason.LATE_DELIVERY].code == "NOT_LATE"
    assert actions.change_address.code == "TOO_LATE_TO_CHANGE"


def test_allowed_actions_flag_a_line_too_expensive_to_refund_without_an_operator() -> None:
    order = delivered(5, lines=(line(1, price=4999), line(2, price=34999)))
    damaged_refund = allowed_actions(Facts(order), POLICY, NOW).refund[
        RefundReason.DAMAGED_OR_DEFECTIVE
    ]
    assert (damaged_refund.allowed, damaged_refund.lines) == (True, (1, 2))
    assert damaged_refund.needs_approval  # one unit of line 2 is over the auto-approve limit


def test_allowed_actions_for_a_shipment_in_transit() -> None:
    actions = allowed_actions(Facts(shipped(idle_days=9, lines=(line(price=20000),))), POLICY, NOW)
    assert not actions.return_.allowed
    assert actions.return_.code == "NOT_DELIVERED"
    lost = actions.refund[RefundReason.LOST_IN_TRANSIT]
    assert (lost.allowed, lost.needs_approval) == (True, True)
    assert actions.change_address.needs_approval


# ---------------------------------------------------------------- configuration drives the rules


def test_policy_values_come_from_the_config() -> None:
    stricter = POLICY.model_copy(
        update={
            "returns": POLICY.returns.model_copy(update={"window_days": 14}),
            "refunds": POLICY.refunds.model_copy(update={"auto_approve_limit_cents": 5000}),
        }
    )
    assert decide(return_one(), delivered(15), policy=stricter)[0] is Outcome.DENY
    order = delivered(lines=(line(price=5001),))
    assert decide(damaged(), order, policy=stricter)[0] is Outcome.NEEDS_APPROVAL


def test_decisions_carry_the_policy_version() -> None:
    decision = evaluate(return_one(), Facts(delivered()), POLICY, NOW)
    assert decision.policy_version == POLICY.version == "2026-09-01"


def test_effects_are_typed() -> None:
    decision = evaluate(damaged(), Facts(replace(delivered(40))), POLICY, NOW)
    assert all(hit.effect is Effect.DENY for hit in decision.hits)
