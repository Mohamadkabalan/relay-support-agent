"""Loading orders through the customer's scope, and turning rows into policy facts.

`load_owned_order` is the single doorway to an order. It filters by order number *and* customer,
so an order on another account is indistinguishable from one that doesn't exist (OWN-1).
"""

import uuid
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from relay.db.models import (
    Customer,
    Order,
    OrderLine,
    Refund,
    RefundLine,
    Return,
    ReturnLine,
    Shipment,
    ShipmentEvent,
)
from relay.domain.enums import RefundChannel, RefundLineKind, ReturnStatus
from relay.domain.errors import OrderNotFoundError
from relay.domain.policy import CustomerFacts, Facts, LineFacts, OrderFacts, ShipmentFacts
from relay.domain.policy_config import PolicyConfig


def normalize_order_number(number: str) -> str:
    return number.strip().upper()


async def load_owned_order(
    session: AsyncSession, customer_id: uuid.UUID, number: str, *, lock: bool = False
) -> Order:
    """The customer's order, with its lines, products and shipment, or `ORDER_NOT_FOUND`.

    With `lock=True` the order row is locked until the transaction ends, so two commits on the
    same order (two refunds for the same line, say) run one after the other.
    """
    normalized = normalize_order_number(number)
    query = (
        select(Order)
        .where(Order.number == normalized, Order.customer_id == customer_id)
        .options(
            selectinload(Order.lines).selectinload(OrderLine.product),
            selectinload(Order.shipment),
        )
    )
    if lock:
        query = query.with_for_update(of=Order)
    order = (await session.execute(query)).scalar_one_or_none()
    if order is None:
        raise OrderNotFoundError(normalized)
    return order


async def lock_customer(session: AsyncSession, customer_id: uuid.UUID) -> None:
    """Serialize refund commits per customer, so the velocity rule (REF-6) can't be raced."""
    await session.execute(select(Customer.id).where(Customer.id == customer_id).with_for_update())


async def order_facts(session: AsyncSession, order: Order) -> OrderFacts:
    returned = await _returned_quantities(session, order.id)
    refunded = await _refunded_quantities(session, order.id)
    shipping_refunded = await _shipping_refunded(session, order.id)
    shipment_facts: ShipmentFacts | None = None
    delivered_at: datetime | None = None
    if order.shipment is not None:
        shipment_facts = ShipmentFacts(
            status=order.shipment.status,
            last_scan_at=await _last_scan(session, order.shipment),
        )
        delivered_at = order.shipment.delivered_at
    return OrderFacts(
        number=order.number,
        status=order.status,
        lines=tuple(
            LineFacts(
                line_no=line.line_no,
                sku=line.sku,
                name=line.name,
                unit_price_cents=line.unit_price_cents,
                quantity=line.quantity,
                kind=line.product.kind,
                condition=line.product.condition,
                final_sale=line.product.final_sale,
                returned_qty=returned.get(line.id, 0),
                refunded_qty=refunded.get(line.id, 0),
            )
            for line in order.lines
        ),
        shipping_cents=order.shipping_cents,
        shipping_refunded=shipping_refunded,
        delivered_at=delivered_at,
        promised_delivery_on=order.promised_delivery_on,
        shipment=shipment_facts,
    )


async def customer_facts(
    session: AsyncSession, customer_id: uuid.UUID, policy: PolicyConfig, now: datetime
) -> CustomerFacts:
    since = now - timedelta(days=policy.refunds.velocity_window_days)
    issued = await session.scalars(
        select(Refund.issued_at)
        .join(Order, Order.id == Refund.order_id)
        .where(
            Order.customer_id == customer_id,
            Refund.channel == RefundChannel.AGENT,
            Refund.issued_at >= since,
        )
        .order_by(Refund.issued_at)
    )
    return CustomerFacts(agent_refunds_issued_at=tuple(issued))


async def facts_for(
    session: AsyncSession, order: Order, policy: PolicyConfig, now: datetime
) -> Facts:
    return Facts(
        order=await order_facts(session, order),
        customer=await customer_facts(session, order.customer_id, policy, now),
    )


async def _returned_quantities(session: AsyncSession, order_id: uuid.UUID) -> dict[uuid.UUID, int]:
    rows = await session.execute(
        select(ReturnLine.order_line_id, func.sum(ReturnLine.quantity))
        .join(Return, Return.id == ReturnLine.return_id)
        .where(Return.order_id == order_id, Return.status != ReturnStatus.CANCELLED)
        .group_by(ReturnLine.order_line_id)
    )
    return {line_id: int(qty) for line_id, qty in rows}


async def _refunded_quantities(session: AsyncSession, order_id: uuid.UUID) -> dict[uuid.UUID, int]:
    """Units refunded without a return. Units refunded through a return count as returned."""
    rows = await session.execute(
        select(RefundLine.order_line_id, func.sum(RefundLine.quantity))
        .join(Refund, Refund.id == RefundLine.refund_id)
        .where(
            Refund.order_id == order_id,
            Refund.return_id.is_(None),
            RefundLine.kind == RefundLineKind.ITEM,
        )
        .group_by(RefundLine.order_line_id)
    )
    return {line_id: int(qty or 0) for line_id, qty in rows if line_id is not None}


async def _shipping_refunded(session: AsyncSession, order_id: uuid.UUID) -> bool:
    found = await session.scalar(
        select(func.count())
        .select_from(RefundLine)
        .join(Refund, Refund.id == RefundLine.refund_id)
        .where(Refund.order_id == order_id, RefundLine.kind == RefundLineKind.SHIPPING)
    )
    return bool(found)


async def _last_scan(session: AsyncSession, shipment: Shipment) -> datetime | None:
    return await session.scalar(
        select(func.max(ShipmentEvent.occurred_at)).where(ShipmentEvent.shipment_id == shipment.id)
    )
