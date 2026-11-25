"""Readable numbers for customer-facing records, drawn from Postgres sequences."""

from sqlalchemy import Sequence
from sqlalchemy.ext.asyncio import AsyncSession

from relay.db.models import APPROVAL_NUMBERS, ESCALATION_NUMBERS, REFUND_NUMBERS, RMA_NUMBERS


def rma_number(n: int) -> str:
    return f"RMA-{n:06d}"


def refund_number(n: int) -> str:
    return f"RF-{n:05d}"


def approval_number(n: int) -> str:
    return f"APR-{n:05d}"


def escalation_number(n: int) -> str:
    return f"ESC-{n:05d}"


async def _next(session: AsyncSession, sequence: Sequence) -> int:
    value = await session.scalar(sequence.next_value())
    if value is None:  # pragma: no cover - nextval always returns a value
        raise RuntimeError(f"{sequence.name} returned no value")
    return int(value)


async def next_rma_number(session: AsyncSession) -> str:
    return rma_number(await _next(session, RMA_NUMBERS))


async def next_refund_number(session: AsyncSession) -> str:
    return refund_number(await _next(session, REFUND_NUMBERS))


async def next_approval_number(session: AsyncSession) -> str:
    return approval_number(await _next(session, APPROVAL_NUMBERS))


async def next_escalation_number(session: AsyncSession) -> str:
    return escalation_number(await _next(session, ESCALATION_NUMBERS))
