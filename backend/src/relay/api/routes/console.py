"""The operator console's API (§5.9, §10). Every route needs an operator session.

The demo operator sees only the customers of its own visitor (D4); the real operator sees all.
Anything outside the caller's view answers 404.

An approval decision runs in one transaction: `approvals.resolve` (lock, re-check the policy with
operator authority, execute, audit) plus the inbox entry and the `approval.resolved` event that
schedule the resume turn. After the commit the event is published and the turn is kicked, so
the customer hears the outcome in the chat.

Every operator write is audited with who, what and why: decisions, escalation resolutions,
hand-backs and replies.
"""

import uuid
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from fastapi.sse import EventSourceResponse, ServerSentEvent
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import ColumnElement, func, select, true
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from relay.agent.events import AgentEvent
from relay.agent.store import OPERATOR_REPLY
from relay.api import events
from relay.api.deps import OperatorAuth, OperatorSession, Svc, operator_rate
from relay.api.services import Services
from relay.db.models import (
    Approval,
    Conversation,
    Customer,
    Escalation,
    Message,
    ModelCall,
    ToolCall,
    Turn,
)
from relay.domain import approvals, audit
from relay.domain.enums import (
    ApprovalStatus,
    ConversationStatus,
    EscalationPriority,
    EscalationReason,
    EscalationStatus,
    MessageKind,
    TurnStatus,
    TurnTrigger,
)
from relay.domain.errors import ConflictError, NotFoundError
from relay.domain.escalations import resolve_escalation
from relay.domain.results import ApprovalView
from relay.domain.scope import Deps, OperatorScope
from relay.telemetry.tracing import current_trace_id

router = APIRouter(prefix="/api/console", tags=["console"], dependencies=[Depends(operator_rate)])

NOTE_CHARS = 1000


# ---------------------------------------------------------------- response models


class _Row(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class ConversationRow(BaseModel):
    id: uuid.UUID
    customer_name: str
    status: ConversationStatus
    created_at: datetime
    closed_at: datetime | None
    last_activity_at: datetime
    turns: int
    cost_micros: int
    flags: int
    injection_flags: int
    rating: int | None
    pending_approvals: int
    open_escalation: str | None


class ReplayMessage(_Row):
    seq: int
    kind: MessageKind
    turn_id: uuid.UUID | None
    content: dict[str, Any]
    created_at: datetime


class ReplayModelCall(_Row):
    seq: int
    requested_model: str
    served_model: str
    effort: str | None
    stop_reason: str
    refusal_category: str | None
    fallback_used: bool
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    cost_micros: int | None
    latency_ms: int
    ttft_ms: int | None
    thinking: str | None


class ReplayToolCall(_Row):
    seq: int
    name: str
    arguments: dict[str, Any] | None
    result: str
    is_error: bool
    outcome: str
    error_code: str | None
    latency_ms: int
    attempts: int


class ReplayTurn(_Row):
    id: uuid.UUID
    seq: int
    trigger: TurnTrigger
    status: TurnStatus
    steps: int
    tool_call_count: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    cost_micros: int
    ttft_ms: int | None
    latency_ms: int | None
    retracted: bool
    error: str | None
    trace_id: str | None
    started_at: datetime
    finished_at: datetime | None
    model_calls: list[ReplayModelCall] = []
    tool_calls: list[ReplayToolCall] = []


class ApprovalRow(ApprovalView):
    conversation_id: uuid.UUID | None
    customer_name: str


class EscalationRow(_Row):
    escalation_number: str
    status: EscalationStatus
    reason: EscalationReason
    priority: EscalationPriority
    summary: str
    created_at: datetime
    resolved_at: datetime | None
    conversation_id: uuid.UUID | None
    customer_name: str = ""


class ConversationReplay(BaseModel):
    id: uuid.UUID
    status: ConversationStatus
    customer_name: str
    customer_email: str
    provider: str
    model: str
    effort: str | None
    prompt_version: str
    policy_version: str
    created_at: datetime
    closed_at: datetime | None
    rating: int | None
    flags: list[Any]
    injection_flags: int
    cost_micros: int
    cost_complete: bool
    messages: list[ReplayMessage]
    turns: list[ReplayTurn]
    approvals: list[ApprovalRow]
    escalations: list[EscalationRow]


class Decision(BaseModel):
    decision: Literal["approve", "reject"]
    note: str | None = Field(default=None, max_length=NOTE_CHARS)


class Resolution(BaseModel):
    note: str | None = Field(default=None, max_length=NOTE_CHARS)
    hand_back: bool = False  # give the conversation back to the agent instead of closing it


class Reply(BaseModel):
    text: str


class Sent(BaseModel):
    message_seq: int


# ---------------------------------------------------------------- conversations


@router.get("/conversations")
async def conversations(
    operator: OperatorAuth,
    services: Svc,
    status_filter: Annotated[ConversationStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[ConversationRow]:
    last_message = (
        select(func.max(Message.created_at))
        .where(Message.conversation_id == Conversation.id)
        .scalar_subquery()
    )
    turns = select(func.count(Turn.id)).where(Turn.conversation_id == Conversation.id)
    pending = select(func.count(Approval.id)).where(
        Approval.conversation_id == Conversation.id, Approval.status == ApprovalStatus.PENDING
    )
    escalation = (
        select(Escalation.escalation_number)
        .where(
            Escalation.conversation_id == Conversation.id,
            Escalation.status == EscalationStatus.OPEN,
        )
        .limit(1)
    )
    query = (
        select(
            Conversation,
            Customer.full_name,
            func.coalesce(last_message, Conversation.created_at),
            turns.scalar_subquery(),
            pending.scalar_subquery(),
            escalation.scalar_subquery(),
        )
        .join(Customer, Customer.id == Conversation.customer_id)
        .where(visible(operator, Conversation.customer_id))
        .order_by(Conversation.created_at.desc())
        .limit(limit)
    )
    if status_filter is not None:
        query = query.where(Conversation.status == status_filter)
    async with services.sessionmaker() as db:
        rows = (await db.execute(query)).all()
    return [
        ConversationRow(
            id=conv.id,
            customer_name=name,
            status=conv.status,
            created_at=conv.created_at,
            closed_at=conv.closed_at,
            last_activity_at=last,
            turns=turn_count,
            cost_micros=conv.cost_micros,
            flags=len(conv.flags),
            injection_flags=conv.injection_flags,
            rating=conv.rating,
            pending_approvals=pending_count,
            open_escalation=open_escalation,
        )
        for conv, name, last, turn_count, pending_count, open_escalation in rows
    ]


@router.get("/conversations/{conversation_id}")
async def replay(
    conversation_id: uuid.UUID, operator: OperatorAuth, services: Svc
) -> ConversationReplay:
    """Everything about one conversation: transcript, turns with model and tool calls, costs,
    trace IDs, approvals and escalations."""
    async with services.sessionmaker() as db:
        conversation, customer = await _visible_conversation(db, operator, conversation_id)
        messages = await db.scalars(
            select(Message).where(Message.conversation_id == conversation_id).order_by(Message.seq)
        )
        turn_rows = list(
            await db.scalars(
                select(Turn).where(Turn.conversation_id == conversation_id).order_by(Turn.seq)
            )
        )
        turn_ids = [turn.id for turn in turn_rows]
        model_calls = await db.scalars(
            select(ModelCall).where(ModelCall.turn_id.in_(turn_ids)).order_by(ModelCall.seq)
        )
        tool_calls = await db.scalars(
            select(ToolCall).where(ToolCall.turn_id.in_(turn_ids)).order_by(ToolCall.seq)
        )
        turns = {turn.id: ReplayTurn.model_validate(turn) for turn in turn_rows}
        for call in model_calls:
            turns[call.turn_id].model_calls.append(ReplayModelCall.model_validate(call))
        for tool in tool_calls:
            turns[tool.turn_id].tool_calls.append(ReplayToolCall.model_validate(tool))
        approval_rows = await _approval_rows(
            db, services, select(Approval).where(Approval.conversation_id == conversation_id)
        )
        escalation_rows = [
            EscalationRow.model_validate(row).model_copy(
                update={"customer_name": customer.full_name}
            )
            for row in await db.scalars(
                select(Escalation)
                .where(Escalation.conversation_id == conversation_id)
                .order_by(Escalation.created_at)
            )
        ]
    return ConversationReplay(
        id=conversation.id,
        status=conversation.status,
        customer_name=customer.full_name,
        customer_email=customer.email,
        provider=conversation.provider,
        model=conversation.model,
        effort=conversation.effort,
        prompt_version=conversation.prompt_version,
        policy_version=conversation.policy_version,
        created_at=conversation.created_at,
        closed_at=conversation.closed_at,
        rating=conversation.rating,
        flags=list(conversation.flags),
        injection_flags=conversation.injection_flags,
        cost_micros=conversation.cost_micros,
        cost_complete=conversation.cost_complete,
        messages=[ReplayMessage.model_validate(m) for m in messages],
        turns=list(turns.values()),
        approvals=approval_rows,
        escalations=escalation_rows,
    )


async def visible_conversation(
    conversation_id: uuid.UUID, operator: OperatorAuth, services: Svc
) -> Conversation:
    """A dependency, so the 404 is decided before a streaming response starts."""
    async with services.sessionmaker() as db:
        conversation, _ = await _visible_conversation(db, operator, conversation_id)
    return conversation


@router.get("/conversations/{conversation_id}/events", response_class=EventSourceResponse)
async def stream_events(
    conversation: Annotated[Conversation, Depends(visible_conversation)],
    services: Svc,
    last_event_id: Annotated[str | None, Header()] = None,
    after: Annotated[int | None, Query(ge=0)] = None,
) -> AsyncIterator[ServerSentEvent]:
    start = events.parse_last_event_id(last_event_id, after)
    async for event in events.stream(
        services.bus, services.store, conversation.id, start, lambda _type: True
    ):
        yield event


@router.post("/conversations/{conversation_id}/messages", status_code=status.HTTP_201_CREATED)
async def reply(
    conversation_id: uuid.UUID, body: Reply, operator: OperatorAuth, services: Svc
) -> Sent:
    """Write to the customer in an escalated conversation. The agent never sees this text as
    its own; the next turn relays it as a trusted note."""
    text = body.text.strip()
    if not text or len(text) > services.settings.max_message_chars:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"Replies are 1 to {services.settings.max_message_chars} characters.",
        )
    async with services.sessionmaker() as db, db.begin():
        conversation, _ = await _visible_conversation(db, operator, conversation_id, lock=True)
        if conversation.status is not ConversationStatus.ESCALATED:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "Only an escalated conversation takes operator replies; the agent owns this one.",
            )
        seq = (
            await db.scalar(
                select(func.max(Message.seq)).where(Message.conversation_id == conversation_id)
            )
            or 0
        ) + 1
        db.add(
            Message(
                conversation_id=conversation_id,
                seq=seq,
                turn_id=None,
                kind=MessageKind.NOTICE,
                content={"text": text, "reason": OPERATOR_REPLY, "author": operator.name},
                raw=None,
                created_at=services.clock.now(),
            )
        )
        audit.record(
            _deps(db, services),
            _scope(operator),
            action="conversation.reply",
            entity="conversation",
            entity_id=str(conversation_id),
            on_behalf_of=conversation.customer_id,
            after={"message_seq": seq},
            conversation_id=conversation_id,
        )
        event = await services.store.emit(
            conversation_id,
            None,
            "message.completed",
            {"message_seq": seq, "text": text, "notice": OPERATOR_REPLY, "author": "team"},
            session=db,
        )
    await services.bus.publish(event)
    return Sent(message_seq=seq)


# ---------------------------------------------------------------- approvals


@router.get("/approvals")
async def approval_queue(
    operator: OperatorAuth,
    services: Svc,
    status_filter: Annotated[ApprovalStatus | None, Query(alias="status")] = ApprovalStatus.PENDING,
) -> list[ApprovalRow]:
    query = (
        select(Approval)
        .where(visible(operator, Approval.customer_id))
        .order_by(Approval.requested_at)
        .limit(200)
    )
    if status_filter is not None:
        query = query.where(Approval.status == status_filter)
    async with services.sessionmaker() as db:
        return await _approval_rows(db, services, query)


@router.get("/approvals/{approval_number}")
async def approval(approval_number: str, operator: OperatorAuth, services: Svc) -> ApprovalRow:
    async with services.sessionmaker() as db:
        row = await _visible_approval(db, operator, approval_number)
        return (await _approval_rows(db, services, select(Approval).where(Approval.id == row.id)))[
            0
        ]


@router.post("/approvals/{approval_number}/decision")
async def decide(
    approval_number: str, body: Decision, operator: OperatorAuth, services: Svc
) -> ApprovalRow:
    """Approve (re-check, execute, audit) or reject (with a note); the customer is told in chat."""
    note = (body.note or "").strip() or None
    if body.decision == "reject" and note is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "A rejection needs a note: the agent explains it to the customer.",
        )
    event: AgentEvent | None = None
    async with services.sessionmaker() as db, db.begin():
        row = await _visible_approval(db, operator, approval_number)
        try:
            view = await approvals.resolve(
                _deps(db, services),
                _scope(operator),
                row.approval_number,
                approve=body.decision == "approve",
                note=note,
            )
        except ConflictError as error:
            raise HTTPException(status.HTTP_409_CONFLICT, error.message) from error
        if row.conversation_id is not None:
            event = await services.store.enqueue_approval(row.conversation_id, view, db)
        result = (
            await _approval_rows(db, services, select(Approval).where(Approval.id == row.id))
        )[0]
    if event is not None:
        await services.bus.publish(event)
        services.kick(event.conversation_id)
    return result


# ---------------------------------------------------------------- escalations


@router.get("/escalations")
async def escalations(
    operator: OperatorAuth,
    services: Svc,
    status_filter: Annotated[
        EscalationStatus | None, Query(alias="status")
    ] = EscalationStatus.OPEN,
) -> list[EscalationRow]:
    query = (
        select(Escalation, Customer.full_name)
        .join(Customer, Customer.id == Escalation.customer_id)
        .where(visible(operator, Escalation.customer_id))
        .order_by(Escalation.created_at)
        .limit(200)
    )
    if status_filter is not None:
        query = query.where(Escalation.status == status_filter)
    async with services.sessionmaker() as db:
        rows = (await db.execute(query)).all()
    return [
        EscalationRow.model_validate(row).model_copy(update={"customer_name": name})
        for row, name in rows
    ]


@router.post("/escalations/{escalation_number}/resolve")
async def resolve(
    escalation_number: str, body: Resolution, operator: OperatorAuth, services: Svc
) -> EscalationRow:
    """Resolve the case, then close the conversation or hand it back to the agent (§5.3)."""
    note = (body.note or "").strip() or None
    published: AgentEvent | None = None
    async with services.sessionmaker() as db, db.begin():
        row = await db.scalar(
            select(Escalation).where(
                Escalation.escalation_number == escalation_number.strip().upper(),
                visible(operator, Escalation.customer_id),
            )
        )
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "No such escalation.")
        deps, scope = _deps(db, services), _scope(operator)
        try:
            await resolve_escalation(deps, scope, row.escalation_number, note)
        except ConflictError as error:
            raise HTTPException(status.HTTP_409_CONFLICT, error.message) from error
        if row.conversation_id is not None:
            published = await _after_resolution(
                db, services, deps, scope, row, body.hand_back, note
            )
        name = await db.scalar(select(Customer.full_name).where(Customer.id == row.customer_id))
        result = EscalationRow.model_validate(row).model_copy(update={"customer_name": name or ""})
    if published is not None:
        await services.bus.publish(published)
    return result


async def _after_resolution(
    db: AsyncSession,
    services: Services,
    deps: Deps,
    scope: OperatorScope,
    row: Escalation,
    hand_back: bool,
    note: str | None,
) -> AgentEvent | None:
    conversation_id = row.conversation_id
    if conversation_id is None:
        return None
    conversation = await db.get_one(Conversation, conversation_id, with_for_update=True)
    if conversation.status is not ConversationStatus.ESCALATED:
        return None
    new = ConversationStatus.ACTIVE if hand_back else ConversationStatus.CLOSED
    conversation.status = new
    if new is ConversationStatus.CLOSED:
        conversation.closed_at = services.clock.now()
    audit.record(
        deps,
        scope,
        action="conversation.hand_back" if hand_back else "conversation.close",
        entity="conversation",
        entity_id=str(conversation_id),
        on_behalf_of=conversation.customer_id,
        before={"status": ConversationStatus.ESCALATED.value},
        after={"status": new.value},
        reason=note,
        conversation_id=conversation_id,
    )
    return await services.store.emit(
        conversation_id, None, "conversation.updated", {"status": new.value}, session=db
    )


# ---------------------------------------------------------------- helpers


def visible(
    operator: OperatorSession, customer_id: InstrumentedAttribute[uuid.UUID]
) -> ColumnElement[bool]:
    """The demo operator's view: only its visitor's customers. The real operator sees all."""
    if operator.visitor_id is None:
        return true()
    return customer_id.in_(
        select(Customer.id).where(Customer.demo_visitor_id == operator.visitor_id)
    )


async def _visible_conversation(
    db: AsyncSession, operator: OperatorSession, conversation_id: uuid.UUID, *, lock: bool = False
) -> tuple[Conversation, Customer]:
    query = (
        select(Conversation, Customer)
        .join(Customer, Customer.id == Conversation.customer_id)
        .where(Conversation.id == conversation_id, visible(operator, Conversation.customer_id))
    )
    if lock:
        query = query.with_for_update(of=Conversation)
    found = (await db.execute(query)).first()
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such conversation.")
    return found[0], found[1]


async def _visible_approval(db: AsyncSession, operator: OperatorSession, number: str) -> Approval:
    row = await db.scalar(
        select(Approval).where(
            Approval.approval_number == number.strip().upper(),
            visible(operator, Approval.customer_id),
        )
    )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such approval.")
    return row


async def _approval_rows(db: AsyncSession, services: Services, query: Any) -> list[ApprovalRow]:
    deps = _deps(db, services)
    rows = []
    for row in await db.scalars(query):
        try:
            view = await approvals.view(deps, row)
        except NotFoundError:  # pragma: no cover - an approval always has an order
            continue
        name = await db.scalar(select(Customer.full_name).where(Customer.id == row.customer_id))
        rows.append(
            ApprovalRow(
                **view.model_dump(), conversation_id=row.conversation_id, customer_name=name or ""
            )
        )
    return rows


def _deps(db: AsyncSession, services: Services) -> Deps:
    return Deps(session=db, clock=services.clock, policy=services.policy)


def _scope(operator: OperatorSession) -> OperatorScope:
    return OperatorScope(operator_id=operator.operator_id, trace_id=current_trace_id())
