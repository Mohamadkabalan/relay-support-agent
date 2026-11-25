"""create_return and update_shipping_address through the confirm-then-commit protocol."""

import pytest
from sqlalchemy import select
from tests.support.factories import Factory

from relay.db.models import Order, Return
from relay.domain.address import Address
from relay.domain.address_changes import AddressChangeArgs, update_shipping_address
from relay.domain.enums import ReturnReason, ReturnStatus
from relay.domain.errors import LineNotFoundError, OrderNotFoundError
from relay.domain.orders import get_order
from relay.domain.results import (
    AddressChangeCompleted,
    DeniedResult,
    PendingApprovalResult,
    PreviewResult,
    ReturnCompleted,
)
from relay.domain.returns import ReturnArgs, create_return
from relay.domain.scope import Deps

NEW_HOME = Address(
    recipient_name="Marcus Reyes",
    line1="901 West 6th Street",
    line2="Apt 12",
    city="Austin",
    state="TX",
    postal_code="78703",
)


def return_args(number: str, lines: dict[int, int], details: str | None = None) -> ReturnArgs:
    return ReturnArgs(
        order_number=number, lines=lines, reason=ReturnReason.CHANGED_MIND, details=details
    )


async def test_return_creates_an_rma_with_a_label(deps: Deps, factory: Factory) -> None:
    me = await factory.customer()
    order = await factory.order(me, lines=[("ACC-USBC1M", 3), ("AUD-BUDS2", 1)])
    first, second = factory.turns(me)

    shown = await create_return(deps, first, return_args(order.number, {1: 2, 2: 1}))
    assert isinstance(shown, PreviewResult)
    assert shown.amount_cents == 2 * 999 + 12999
    assert "2 x Volt USB-C Cable 1 m, 1 x Aurora Buds 2" in shown.summary

    done = await create_return(
        deps, second, return_args(order.number, {1: 2, 2: 1}, "Too many"), shown.confirmation_token
    )
    assert isinstance(done, ReturnCompleted)
    assert done.rma_number.startswith("RMA-")
    assert done.label_url.endswith(f"{done.rma_number}.pdf")
    assert done.refund_estimate_cents == shown.amount_cents
    stored = await deps.session.scalar(select(Return).where(Return.rma_number == done.rma_number))
    assert stored is not None
    assert (stored.status, stored.details) == (ReturnStatus.LABEL_ISSUED, "Too many")
    [audit] = await factory.audit(me)
    assert (audit.action, audit.entity_id) == ("return.create", done.rma_number)

    # The returned units are no longer available.
    detail = await get_order(deps, first, order.number)
    assert [line.refundable_quantity for line in detail.lines] == [1, 0]
    again = await create_return(deps, factory.turns(me)[0], return_args(order.number, {2: 1}))
    assert isinstance(again, DeniedResult)
    assert again.code == "QUANTITY_UNAVAILABLE"


async def test_return_after_the_window_is_denied_with_an_alternative(
    deps: Deps, factory: Factory
) -> None:
    me = await factory.customer()
    order = await factory.order(me, delivered_days_ago=31)
    result = await create_return(deps, factory.turns(me)[0], return_args(order.number, {1: 1}))
    assert isinstance(result, DeniedResult)
    assert (result.rule, result.code) == ("RET-2", "RETURN_WINDOW_EXPIRED")
    assert result.alternatives


async def test_return_of_a_line_that_does_not_exist(deps: Deps, factory: Factory) -> None:
    me = await factory.customer()
    order = await factory.order(me)
    with pytest.raises(LineNotFoundError):
        await create_return(deps, factory.turns(me)[0], return_args(order.number, {9: 1}))


async def test_return_of_someone_elses_order(deps: Deps, factory: Factory) -> None:
    me, other = await factory.customer(), await factory.customer()
    theirs = await factory.order(other)
    with pytest.raises(OrderNotFoundError):
        await create_return(deps, factory.turns(me)[0], return_args(theirs.number, {1: 1}))


async def test_address_change_before_shipping_is_immediate(deps: Deps, factory: Factory) -> None:
    me = await factory.customer("Marcus Reyes")
    order = await factory.order(me, "processing")
    first, second = factory.turns(me)
    shown = await update_shipping_address(
        deps, first, AddressChangeArgs(order_number=order.number, address=NEW_HOME)
    )
    assert isinstance(shown, PreviewResult)
    assert shown.amount_cents is None
    assert shown.summary.endswith("901 West 6th Street, Apt 12, Austin, TX 78703.")

    # The customer confirms; the model resends the same address with different spacing.
    spaced = NEW_HOME.model_copy(update={"line1": "901  West 6th Street ", "state": "tx"})
    done = await update_shipping_address(
        deps,
        second,
        AddressChangeArgs(order_number=order.number, address=spaced),
        shown.confirmation_token,
    )
    assert isinstance(done, AddressChangeCompleted)
    stored = await deps.session.get_one(Order, order.id)
    assert stored.shipping_address["line1"] == "901 West 6th Street"
    [audit] = await factory.audit(me)
    assert audit.before is not None
    assert audit.after is not None
    assert audit.before["shipping_address"]["line1"] == "12 Test Street"
    assert audit.after["shipping_address"]["city"] == "Austin"


async def test_address_change_in_transit_needs_approval(deps: Deps, factory: Factory) -> None:
    me = await factory.customer()
    order = await factory.order(me, "in_transit")
    first, second = factory.turns(me)
    args = AddressChangeArgs(order_number=order.number, address=NEW_HOME)
    shown = await update_shipping_address(deps, first, args)
    assert isinstance(shown, PreviewResult)
    assert shown.needs_operator_approval
    pending = await update_shipping_address(deps, second, args, shown.confirmation_token)
    assert isinstance(pending, PendingApprovalResult)
    stored = await deps.session.get_one(Order, order.id)
    assert stored.shipping_address["city"] == "Springfield"  # unchanged until approved


@pytest.mark.parametrize(
    ("kind", "country", "code"),
    [
        ("delivered", "US", "TOO_LATE_TO_CHANGE"),
        ("out_for_delivery", "US", "TOO_LATE_TO_CHANGE"),
        ("processing", "CA", "INTERNATIONAL_NOT_SUPPORTED"),
    ],
)
async def test_address_change_denials(
    deps: Deps, factory: Factory, kind: str, country: str, code: str
) -> None:
    me = await factory.customer()
    order = await factory.order(me, kind)
    address = factory.address("Somebody", country=country)
    result = await update_shipping_address(
        deps, factory.turns(me)[0], AddressChangeArgs(order_number=order.number, address=address)
    )
    assert isinstance(result, DeniedResult)
    assert result.code == code


async def test_address_change_on_someone_elses_order(deps: Deps, factory: Factory) -> None:
    me, other = await factory.customer(), await factory.customer()
    theirs = await factory.order(other, "processing")
    with pytest.raises(OrderNotFoundError):
        await update_shipping_address(
            deps,
            factory.turns(me)[0],
            AddressChangeArgs(order_number=theirs.number, address=NEW_HOME),
        )
    stored = await deps.session.get_one(Order, theirs.id)
    assert stored.shipping_address["city"] == "Springfield"
