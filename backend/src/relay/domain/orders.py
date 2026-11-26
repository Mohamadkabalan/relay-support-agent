"""Read services behind get_customer, list_orders, get_order and track_shipment.

Every query is scoped to the customer in the `CustomerScope`.
"""

from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from relay.clock import local_date
from relay.db.models import Address as AddressRow
from relay.db.models import (
    Approval,
    Customer,
    Escalation,
    Order,
    OrderLine,
    Refund,
    Return,
    ShipmentEvent,
)
from relay.domain.address import Address
from relay.domain.enums import (
    OPEN_RETURN_STATUSES,
    ApprovalStatus,
    EscalationStatus,
    OrderStatus,
    ShipmentStatus,
)
from relay.domain.errors import InvalidArgumentError, NotFoundError
from relay.domain.facts import facts_for, load_owned_order
from relay.domain.policy import (
    AllowedActions,
    Availability,
    Facts,
    LineFacts,
    allowed_actions,
    days_without_scan,
    is_lost,
    return_window_ends_on,
)
from relay.domain.results import (
    AllowedActionsView,
    AvailabilityView,
    CustomerProfile,
    OrderDetail,
    OrderLineView,
    OrderList,
    OrderSummary,
    PendingRequestView,
    RefundView,
    ReturnView,
    ShipmentSummary,
    Tracking,
    TrackingEvent,
    UntrustedText,
)
from relay.domain.scope import CustomerScope, Deps
from relay.domain.writes import payment_destination

MAX_LIST_LIMIT = 20


async def get_customer(deps: Deps, scope: CustomerScope) -> CustomerProfile:
    customer = await deps.session.scalar(
        select(Customer)
        .where(Customer.id == scope.customer_id)
        .options(selectinload(Customer.addresses))
    )
    if customer is None:  # the scope came from a verified token, so this means stale data
        raise NotFoundError("The signed-in customer no longer exists.")
    default = next((row for row in customer.addresses if row.is_default), None)
    open_returns = await deps.session.scalar(
        select(func.count())
        .select_from(Return)
        .join(Order, Order.id == Return.order_id)
        .where(Order.customer_id == customer.id, Return.status.in_(OPEN_RETURN_STATUSES))
    )
    pending_approvals = await deps.session.scalar(
        select(func.count())
        .select_from(Approval)
        .where(Approval.customer_id == customer.id, Approval.status == ApprovalStatus.PENDING)
    )
    open_escalations = await deps.session.scalar(
        select(func.count())
        .select_from(Escalation)
        .where(Escalation.customer_id == customer.id, Escalation.status == EscalationStatus.OPEN)
    )
    return CustomerProfile(
        full_name=customer.full_name,
        email=customer.email,
        member_since=local_date(customer.created_at, deps.policy.tz),
        default_address=_address(default, customer.full_name) if default else None,
        open_returns=open_returns or 0,
        open_requests=(pending_approvals or 0) + (open_escalations or 0),
    )


async def list_orders(
    deps: Deps,
    scope: CustomerScope,
    *,
    status: OrderStatus | None = None,
    placed_after: date | None = None,
    limit: int = 10,
) -> OrderList:
    if not 1 <= limit <= MAX_LIST_LIMIT:
        raise InvalidArgumentError(
            f"limit must be between 1 and {MAX_LIST_LIMIT}.", "Ask for fewer orders at a time."
        )
    query = (
        select(Order)
        .where(Order.customer_id == scope.customer_id)
        .options(selectinload(Order.lines), selectinload(Order.shipment))
        .order_by(Order.placed_at.desc(), Order.number)
        .limit(limit)
    )
    if status is not None:
        query = query.where(Order.status == status)
    if placed_after is not None:
        query = query.where(
            func.timezone(deps.policy.store_timezone, Order.placed_at) >= placed_after
        )
    orders = (await deps.session.scalars(query)).all()
    summaries = [
        OrderSummary(
            number=order.number,
            placed_on=local_date(order.placed_at, deps.policy.tz),
            status=order.status,
            total_cents=order.total_cents,
            item_count=sum(line.quantity for line in order.lines),
            first_items=[line.name for line in order.lines[:2]],
            shipment_status=order.shipment.status if order.shipment else None,
        )
        for order in orders
    ]
    return OrderList(orders=summaries)


async def get_order(deps: Deps, scope: CustomerScope, order_number: str) -> OrderDetail:
    session = deps.session
    order = await load_owned_order(session, scope.customer_id, order_number)
    now = deps.clock.now()
    facts = await facts_for(session, order, deps.policy, now)
    line_facts = {line.line_no: line for line in facts.order.lines}
    line_numbers = {line.id: line.line_no for line in order.lines}

    returns = (
        await session.scalars(
            select(Return)
            .where(Return.order_id == order.id)
            .options(selectinload(Return.lines))
            .order_by(Return.created_at)
        )
    ).all()
    refunds = (
        await session.scalars(
            select(Refund).where(Refund.order_id == order.id).order_by(Refund.issued_at)
        )
    ).all()
    pending = (
        await session.scalars(
            select(Approval)
            .where(Approval.order_id == order.id, Approval.status == ApprovalStatus.PENDING)
            .order_by(Approval.requested_at)
        )
    ).all()

    return OrderDetail(
        number=order.number,
        order_status=order.status,
        placed_at=order.placed_at,
        promised_delivery_on=order.promised_delivery_on,
        lines=[_line_view(line, line_facts[line.line_no], facts, deps) for line in order.lines],
        shipping_cents=order.shipping_cents,
        total_cents=order.total_cents,
        refunded_cents=sum(refund.amount_cents for refund in refunds),
        payment=payment_destination(order),
        shipping_address=Address.model_validate(order.shipping_address),
        shipment=(
            ShipmentSummary(
                carrier=order.shipment.carrier,
                tracking_number=order.shipment.tracking_number,
                status=order.shipment.status,
                shipped_at=order.shipment.shipped_at,
                delivered_at=order.shipment.delivered_at,
                eta=order.shipment.eta,
            )
            if order.shipment
            else None
        ),
        returns=[
            ReturnView(
                rma_number=row.rma_number,
                status=row.status,
                reason=row.reason,
                created_at=row.created_at,
                lines={line_numbers[line.order_line_id]: line.quantity for line in row.lines},
            )
            for row in returns
        ],
        refunds=[
            RefundView(
                refund_number=row.refund_number,
                reason=row.reason,
                amount_cents=row.amount_cents,
                issued_at=row.issued_at,
            )
            for row in refunds
        ],
        pending_requests=[
            PendingRequestView(
                approval_number=row.approval_number,
                action=row.action,
                summary=row.summary,
                requested_at=row.requested_at,
            )
            for row in pending
        ],
        customer_note=(
            UntrustedText(untrusted_text=order.customer_note, source="order_note")
            if order.customer_note
            else None
        ),
        allowed_actions=_allowed_view(allowed_actions(facts, deps.policy, now)),
    )


async def track_shipment(deps: Deps, scope: CustomerScope, order_number: str) -> Tracking:
    order = await load_owned_order(deps.session, scope.customer_id, order_number)
    shipment = order.shipment
    if shipment is None:
        raise InvalidArgumentError(
            f"Order {order.number} hasn't shipped yet, so there is no tracking.",
            "Call get_order to see its status.",
        )
    events = (
        await deps.session.scalars(
            select(ShipmentEvent)
            .where(ShipmentEvent.shipment_id == shipment.id)
            .order_by(ShipmentEvent.occurred_at.desc())
        )
    ).all()
    now = deps.clock.now()
    facts = await facts_for(deps.session, order, deps.policy, now)
    idle = (
        days_without_scan(facts.order.shipment, deps.policy, now)
        if facts.order.shipment and shipment.status is not ShipmentStatus.DELIVERED
        else None
    )
    return Tracking(
        order_number=order.number,
        carrier=shipment.carrier,
        tracking_number=shipment.tracking_number,
        shipment_status=shipment.status,
        eta=shipment.eta,
        delivered_at=shipment.delivered_at,
        days_since_last_scan=idle,
        stalled=is_lost(facts.order, deps.policy, now),
        events=[
            TrackingEvent(
                occurred_at=event.occurred_at,
                code=event.code,
                description=UntrustedText(untrusted_text=event.description, source="carrier_event"),
                location=event.location,
            )
            for event in events
        ],
    )


def _line_view(line: OrderLine, facts_line: LineFacts, facts: Facts, deps: Deps) -> OrderLineView:
    ends = return_window_ends_on(facts.order, facts_line, deps.policy)
    return OrderLineView(
        line=line.line_no,
        sku=line.sku,
        name=line.name,
        quantity=line.quantity,
        unit_price_cents=line.unit_price_cents,
        refundable_quantity=facts_line.available_qty,
        refundable_cents=facts_line.available_qty * line.unit_price_cents,
        return_window_ends_on=ends,
        final_sale=line.product.final_sale,
        condition=line.product.condition.value,
    )


def _availability(availability: Availability) -> AvailabilityView:
    return AvailabilityView(
        allowed=availability.allowed,
        needs_approval=availability.needs_approval,
        code=availability.code,
        window_ends_on=availability.window_ends_on,
        lines=list(availability.lines),
    )


def _allowed_view(actions: AllowedActions) -> AllowedActionsView:
    return AllowedActionsView(
        return_=_availability(actions.return_),
        refund={reason: _availability(value) for reason, value in actions.refund.items()},
        change_address=_availability(actions.change_address),
    )


def _address(row: AddressRow, fallback_name: str) -> Address:
    return Address(
        recipient_name=row.recipient_name or fallback_name,
        line1=row.line1,
        line2=row.line2,
        city=row.city,
        state=row.state,
        postal_code=row.postal_code,
        country=row.country,
    )
