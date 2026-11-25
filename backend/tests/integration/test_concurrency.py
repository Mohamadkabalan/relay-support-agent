"""Two commits racing for the same unit: row locks make one of them see the other's refund."""

import asyncio

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from tests.support.factories import NOW, Factory, make_engine, temporary_database

from relay.clock import FrozenClock
from relay.db.models import Refund
from relay.domain.enums import RefundReason
from relay.domain.policy_config import default_policy
from relay.domain.refunds import RefundArgs, issue_refund
from relay.domain.results import DeniedResult, PreviewResult, RefundCompleted, WriteResult
from relay.domain.scope import CustomerScope, Deps


async def test_one_unit_is_refunded_once_even_under_concurrent_commits() -> None:
    clock = FrozenClock(NOW)
    async with temporary_database(catalog=True) as url:
        engine = make_engine(url)
        try:
            args = None
            async with AsyncSession(engine, expire_on_commit=False) as session, session.begin():
                factory = Factory(session, clock, default_policy())
                me = await factory.customer()
                order = await factory.order(me, lines=[("ACC-CHG65", 1)])
                first, second = factory.turns(me)
                args = RefundArgs(
                    order_number=order.number,
                    reason=RefundReason.DAMAGED_OR_DEFECTIVE,
                    lines={1: 1},
                )
                deps = Deps(session, clock)
                tokens = []
                for _ in range(2):
                    shown = await issue_refund(deps, first, args)
                    assert isinstance(shown, PreviewResult)
                    tokens.append(shown.confirmation_token)

            async def commit(token: str, scope: CustomerScope) -> WriteResult:
                async with AsyncSession(engine) as session, session.begin():
                    return await issue_refund(Deps(session, clock), scope, args, token)

            results = await asyncio.gather(*(commit(token, second) for token in tokens))
            kinds = sorted(type(result).__name__ for result in results)
            assert kinds == [DeniedResult.__name__, RefundCompleted.__name__]
            denied = next(r for r in results if isinstance(r, DeniedResult))
            assert denied.code == "ALREADY_REFUNDED"

            async with AsyncSession(engine) as session:
                count = await session.scalar(select(func.count()).select_from(Refund))
            assert count == 1
        finally:
            await engine.dispose()
