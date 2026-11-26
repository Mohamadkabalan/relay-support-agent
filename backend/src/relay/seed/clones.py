"""Per-visitor copies of the demo personas (D4).

In demo mode every visitor gets a private copy of the persona they pick: the same story, dated
relative to now, with its own email (`dana.whitfield+<visitor>@example.com`) and order numbers
from `demo_order_number_seq` (PV-801000 to PV-899999). Visitors never see each other's
conversations or orders, and the demo operator console shows only the visitor's own (§12).

Outside demo mode the persona login signs in as the seeded persona itself.
"""

import secrets
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from relay.db.models import DEMO_ORDER_NUMBERS, Customer
from relay.domain.policy_config import PolicyConfig
from relay.seed.builders import Ids
from relay.seed.personas import PERSONAS, Clone, Persona, build_personas


class UnknownPersonaError(LookupError):
    pass


def persona(key: str) -> Persona:
    found = next((p for p in PERSONAS if p.key == key), None)
    if found is None:
        raise UnknownPersonaError(key)
    return found


def new_visitor_id() -> str:
    return secrets.token_hex(5)


async def visitor_copy(
    session: AsyncSession, key: str, visitor_id: str, now: datetime, policy: PolicyConfig
) -> Customer:
    """The visitor's copy of a persona, made on first use. Runs in the caller's transaction."""
    chosen = persona(key)
    email = Clone(visitor_id, {}).email(chosen.email)
    existing = await session.scalar(select(Customer).where(Customer.email == email))
    if existing is not None:
        return existing
    planned = build_personas(Ids(), now, policy, only={key})
    numbers = list(
        await session.scalars(
            select(DEMO_ORDER_NUMBERS.next_value()).select_from(
                func.generate_series(1, len(planned.orders))
            )
        )
    )
    clone = Clone(
        visitor_id,
        {order.number: f"PV-{n:06d}" for order, n in zip(planned.orders, numbers, strict=True)},
    )
    rows = build_personas(Ids(), now, policy, only={key}, clone=clone)
    session.add_all([*rows.customers, *rows.orders])
    await session.flush()
    return rows.customers[0]


async def seeded_persona(session: AsyncSession, key: str) -> Customer | None:
    """The seeded persona itself (outside demo mode)."""
    return await session.scalar(select(Customer).where(Customer.email == persona(key).email))
