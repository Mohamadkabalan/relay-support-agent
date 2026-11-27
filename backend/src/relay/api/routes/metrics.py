"""The operator dashboard's numbers (§10), computed with SQL over Postgres. No analytics store.

Everything is limited to the caller's view (the demo operator sees only its visitor's
customers) and to conversations started in the last `days` days. Each metric carries its
definition, so the dashboard can show it next to the number.
"""

from datetime import date, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import Float, and_, cast, exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from relay.api.deps import OperatorAuth, Svc, operator_rate
from relay.api.routes.console import visible
from relay.db.models import Approval, Conversation, Escalation, ModelCall, ToolCall, Turn
from relay.domain.enums import ApprovalStatus, ConversationStatus

router = APIRouter(
    prefix="/api/console/metrics", tags=["console"], dependencies=[Depends(operator_rate)]
)

DEFINITIONS = {
    "resolution_rate": (
        "Closed conversations that were not escalated, have no open approval and were not rated "
        "thumbs-down, divided by all closed conversations. A proxy."
    ),
    "escalation_rate": "Conversations with an escalation, divided by all conversations.",
    "approval_turnaround": (
        "Time from an approval request to the operator's decision (expiries excluded)."
    ),
    "tool_error_rate": (
        "Tool calls that returned an error, divided by all tool calls. Policy denials are "
        "successful results and are counted separately."
    ),
    "cost_per_conversation": "The summed cost of every model call in a conversation.",
    "thumbs_up_rate": "Thumbs up divided by all ratings.",
    "time_to_first_token": "From sending a model request to its first streamed token.",
    "cache_share": "Input tokens read from the prompt cache, divided by all input tokens.",
}


class DayCount(BaseModel):
    day: date
    conversations: int


class ToolErrors(BaseModel):
    tool: str
    calls: int
    errors: int
    denials: int
    error_codes: dict[str, int]


class Rate(BaseModel):
    numerator: int
    denominator: int
    value: float | None  # None when the denominator is zero


class Spread(BaseModel):
    count: int
    mean: float | None
    p50: float | None
    p90: float | None
    p95: float | None


class Metrics(BaseModel):
    days: int
    conversations: int
    conversations_per_day: list[DayCount]
    resolution_rate: Rate
    escalation_rate: Rate
    approval_turnaround_s: Spread
    tool_error_rate: Rate
    tools: list[ToolErrors]
    cost_per_conversation_micros: Spread
    thumbs_up_rate: Rate
    time_to_first_token_ms: Spread
    cache_share: Rate
    refusals: int
    fallbacks: int
    injection_flags: int
    definitions: dict[str, str]


@router.get("")
async def metrics(
    operator: OperatorAuth, services: Svc, days: Annotated[int, Query(ge=1, le=90)] = 14
) -> Metrics:
    since = services.clock.now() - timedelta(days=days)
    tz = services.policy.store_timezone
    in_view = and_(Conversation.created_at >= since, visible(operator, Conversation.customer_id))
    conversations = select(Conversation.id).where(in_view)
    async with services.sessionmaker() as db:
        per_day = await _per_day(db, in_view, tz)
        total = sum(day.conversations for day in per_day)
        return Metrics(
            days=days,
            conversations=total,
            conversations_per_day=per_day,
            resolution_rate=await _resolution(db, in_view),
            escalation_rate=await _escalation(db, in_view, total),
            approval_turnaround_s=await _turnaround(db, conversations),
            tool_error_rate=await _tool_rate(db, conversations),
            tools=await _tools(db, conversations),
            cost_per_conversation_micros=await _spread(db, Conversation.cost_micros, in_view),
            thumbs_up_rate=await _thumbs(db, in_view),
            time_to_first_token_ms=await _ttft(db, conversations),
            cache_share=await _cache(db, conversations),
            refusals=await _count_calls(db, conversations, ModelCall.stop_reason == "refusal"),
            fallbacks=await _count_calls(db, conversations, ModelCall.fallback_used.is_(True)),
            injection_flags=int(
                await db.scalar(
                    select(func.coalesce(func.sum(Conversation.injection_flags), 0)).where(in_view)
                )
                or 0
            ),
            definitions=DEFINITIONS,
        )


def _rate(numerator: int, denominator: int) -> Rate:
    return Rate(
        numerator=numerator,
        denominator=denominator,
        value=numerator / denominator if denominator else None,
    )


async def _per_day(db: AsyncSession, in_view: Any, tz: str) -> list[DayCount]:
    local_day = func.date(func.timezone(tz, Conversation.created_at))
    rows = await db.execute(
        select(local_day, func.count()).where(in_view).group_by(local_day).order_by(local_day)
    )
    return [DayCount(day=d, conversations=n) for d, n in rows.all()]


async def _resolution(db: AsyncSession, in_view: Any) -> Rate:
    escalated = exists().where(Escalation.conversation_id == Conversation.id)
    pending = exists().where(
        Approval.conversation_id == Conversation.id, Approval.status == ApprovalStatus.PENDING
    )
    resolved = and_(~escalated, ~pending, func.coalesce(Conversation.rating, 0) >= 0)
    row = (
        await db.execute(
            select(func.count(), func.count().filter(resolved)).where(
                in_view, Conversation.status == ConversationStatus.CLOSED
            )
        )
    ).one()
    return _rate(int(row[1]), int(row[0]))


async def _escalation(db: AsyncSession, in_view: Any, total: int) -> Rate:
    escalated = await db.scalar(
        select(func.count()).where(
            in_view, exists().where(Escalation.conversation_id == Conversation.id)
        )
    )
    return _rate(int(escalated or 0), total)


def _quantiles(column: Any) -> list[Any]:
    return [
        func.count(column),
        func.avg(column),
        *(func.percentile_cont(q).within_group(column) for q in (0.5, 0.9, 0.95)),
    ]


def _as_spread(row: Any) -> Spread:
    count, mean, p50, p90, p95 = row
    values = [None if v is None else float(v) for v in (mean, p50, p90, p95)]
    return Spread(count=int(count), mean=values[0], p50=values[1], p90=values[2], p95=values[3])


async def _spread(db: AsyncSession, column: Any, where: Any) -> Spread:
    return _as_spread((await db.execute(select(*_quantiles(column)).where(where))).one())


async def _turnaround(db: AsyncSession, conversations: Any) -> Spread:
    seconds = cast(func.extract("epoch", Approval.decided_at - Approval.requested_at), Float)
    where = and_(
        Approval.conversation_id.in_(conversations),
        Approval.decided_at.is_not(None),
        Approval.status != ApprovalStatus.EXPIRED,
    )
    return _as_spread((await db.execute(select(*_quantiles(seconds)).where(where))).one())


def _calls_in(conversations: Any) -> Any:
    return ToolCall.turn_id.in_(select(Turn.id).where(Turn.conversation_id.in_(conversations)))


# Tool results are stored compactly with the server's field order, and `status` comes first.
DENIED = '{"status":"denied"%'


async def _tool_rate(db: AsyncSession, conversations: Any) -> Rate:
    row = (
        await db.execute(
            select(func.count(), func.count().filter(ToolCall.is_error.is_(True))).where(
                _calls_in(conversations)
            )
        )
    ).one()
    return _rate(int(row[1]), int(row[0]))


async def _tools(db: AsyncSession, conversations: Any) -> list[ToolErrors]:
    rows = await db.execute(
        select(
            ToolCall.name,
            func.count(),
            func.count().filter(ToolCall.is_error.is_(True)),
            func.count().filter(ToolCall.result.like(DENIED)),
        )
        .where(_calls_in(conversations))
        .group_by(ToolCall.name)
        .order_by(ToolCall.name)
    )
    codes = await db.execute(
        select(ToolCall.name, ToolCall.error_code, func.count())
        .where(_calls_in(conversations), ToolCall.is_error.is_(True))
        .group_by(ToolCall.name, ToolCall.error_code)
    )
    by_tool: dict[str, dict[str, int]] = {}
    for name, code, count in codes.all():
        by_tool.setdefault(name, {})[code or "UNKNOWN"] = int(count)
    return [
        ToolErrors(
            tool=name,
            calls=int(calls),
            errors=int(errors),
            denials=int(denials),
            error_codes=by_tool.get(name, {}),
        )
        for name, calls, errors, denials in rows.all()
    ]


async def _thumbs(db: AsyncSession, in_view: Any) -> Rate:
    row = (
        await db.execute(
            select(
                func.count(Conversation.rating), func.count().filter(Conversation.rating > 0)
            ).where(in_view)
        )
    ).one()
    return _rate(int(row[1]), int(row[0]))


def _model_calls_in(conversations: Any) -> Any:
    return ModelCall.turn_id.in_(select(Turn.id).where(Turn.conversation_id.in_(conversations)))


async def _ttft(db: AsyncSession, conversations: Any) -> Spread:
    return await _spread(db, ModelCall.ttft_ms, _model_calls_in(conversations))


async def _cache(db: AsyncSession, conversations: Any) -> Rate:
    row = (
        await db.execute(
            select(
                func.coalesce(func.sum(ModelCall.cache_read_tokens), 0),
                func.coalesce(
                    func.sum(
                        ModelCall.input_tokens
                        + ModelCall.cache_read_tokens
                        + ModelCall.cache_write_tokens
                    ),
                    0,
                ),
            ).where(_model_calls_in(conversations))
        )
    ).one()
    return _rate(int(row[0]), int(row[1]))


async def _count_calls(db: AsyncSession, conversations: Any, condition: Any) -> int:
    count = await db.scalar(select(func.count()).where(_model_calls_in(conversations), condition))
    return int(count or 0)
