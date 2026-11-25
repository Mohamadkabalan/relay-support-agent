"""get_customer, list_orders, get_order and track_shipment: scoped to the signed-in customer."""

from datetime import date

import pytest
from tests.support.factories import Factory

from relay.domain import orders
from relay.domain.enums import OrderStatus, RefundReason, ShipmentStatus
from relay.domain.errors import InvalidArgumentError, OrderNotFoundError
from relay.domain.scope import Deps


async def test_customer_profile(deps: Deps, factory: Factory) -> None:
    dana = await factory.customer("Dana Whitfield")
    profile = await orders.get_customer(deps, factory.scope(dana))
    assert profile.full_name == "Dana Whitfield"
    assert profile.default_address is not None
    assert profile.default_address.city == "Springfield"
    assert (profile.open_returns, profile.open_requests) == (0, 0)


async def test_list_orders_is_scoped_newest_first_and_filterable(
    deps: Deps, factory: Factory
) -> None:
    me, someone_else = await factory.customer(), await factory.customer()
    old = await factory.order(me, "delivered", delivered_days_ago=40)
    new = await factory.order(me, "processing")
    await factory.order(someone_else, "processing")

    listed = await orders.list_orders(deps, factory.scope(me))
    assert [o.number for o in listed] == [new.number, old.number]
    assert listed[0].first_items == ["Aurora Buds 2"]

    processing = await orders.list_orders(deps, factory.scope(me), status=OrderStatus.PROCESSING)
    assert [o.number for o in processing] == [new.number]
    recent = await orders.list_orders(deps, factory.scope(me), placed_after=date(2026, 9, 1))
    assert [o.number for o in recent] == [new.number]


@pytest.mark.parametrize("limit", [0, 21])
async def test_list_orders_limit_is_capped(deps: Deps, factory: Factory, limit: int) -> None:
    me = await factory.customer()
    with pytest.raises(InvalidArgumentError):
        await orders.list_orders(deps, factory.scope(me), limit=limit)


async def test_get_order_shows_lines_payment_note_and_allowed_actions(
    deps: Deps, factory: Factory
) -> None:
    me = await factory.customer()
    order = await factory.order(
        me,
        "delivered",
        lines=[("AUD-ANC700", 1), ("GFT-25", 1)],
        delivered_days_ago=5,
        note="Leave it with the concierge.",
    )
    detail = await orders.get_order(deps, factory.scope(me), order.number.lower())
    assert detail.number == order.number
    assert detail.payment == "Visa ending 4242"
    assert detail.customer_note == "Leave it with the concierge."
    assert [(line.line, line.refundable_cents) for line in detail.lines] == [(1, 34999), (2, 2500)]
    assert detail.lines[0].return_window_ends_on == date(2026, 10, 10)
    actions = detail.allowed_actions
    assert actions.return_.lines == [1]
    assert actions.refund[RefundReason.DAMAGED_OR_DEFECTIVE].allowed
    assert actions.change_address.code == "TOO_LATE_TO_CHANGE"


async def test_track_shipment_lists_scans_newest_first(deps: Deps, factory: Factory) -> None:
    me = await factory.customer()
    order = await factory.order(me, "in_transit", last_scan_days_ago=9)
    tracking = await orders.track_shipment(deps, factory.scope(me), order.number)
    assert tracking.status is ShipmentStatus.IN_TRANSIT
    assert tracking.days_since_last_scan == 9
    assert tracking.stalled
    times = [event.occurred_at for event in tracking.events]
    assert times == sorted(times, reverse=True)
    assert tracking.events[-1].code == "label_created"


async def test_track_shipment_for_an_order_that_has_not_shipped(
    deps: Deps, factory: Factory
) -> None:
    me = await factory.customer()
    order = await factory.order(me, "processing")
    with pytest.raises(InvalidArgumentError, match="hasn't shipped yet"):
        await orders.track_shipment(deps, factory.scope(me), order.number)


@pytest.mark.parametrize("read", ["get_order", "track_shipment"])
async def test_another_customers_order_looks_like_no_order(
    deps: Deps, factory: Factory, read: str
) -> None:
    me, victim = await factory.customer(), await factory.customer()
    theirs = await factory.order(victim, "in_transit")
    service = getattr(orders, read)
    with pytest.raises(OrderNotFoundError) as foreign:
        await service(deps, factory.scope(me), theirs.number)
    with pytest.raises(OrderNotFoundError) as missing:
        await service(deps, factory.scope(me), "PV-999998")
    # Same message shape either way, so the error can't be used to probe for valid numbers.
    assert str(foreign.value).replace(theirs.number, "X") == str(missing.value).replace(
        "PV-999998", "X"
    )
