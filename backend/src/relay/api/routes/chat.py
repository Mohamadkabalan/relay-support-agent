"""The customer's chat (§5.3, §5.10). Every route acts for the session's customer only.

A conversation that isn't the caller's answers 404, exactly like one that doesn't exist, so IDs
can't be probed. Sending a message queues it and returns 202; the reply streams over the events
endpoint. Messages over the size limit get 413 before anything is stored or any model is called.
"""

import uuid
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from fastapi.sse import EventSourceResponse, ServerSentEvent
from pydantic import BaseModel
from sqlalchemy import select

from relay.agent.store import OPERATOR_REPLY, InboxFullError
from relay.api import events
from relay.api.deps import CustomerAuth, Svc, chat_rate, message_rate
from relay.api.services import Services
from relay.db.models import Approval, Conversation, ConversationEvent, Message
from relay.domain.enums import ApprovalStatus, ConversationStatus, MessageKind

router = APIRouter(prefix="/api/conversations", tags=["chat"], dependencies=[Depends(chat_rate)])

Author = Literal["customer", "assistant", "team"]


class ChatMessage(BaseModel):
    seq: int
    author: Author
    text: str
    created_at: datetime
    notice: str | None = None  # why the runtime wrote it: handoff, refusal, failure, ...


class PendingApproval(BaseModel):
    approval_number: str
    summary: str


class ConversationSummary(BaseModel):
    id: uuid.UUID
    status: ConversationStatus
    created_at: datetime
    closed_at: datetime | None


class ConversationView(ConversationSummary):
    rating: Literal["up", "down"] | None
    messages: list[ChatMessage]
    pending_approvals: list[PendingApproval]


class Created(BaseModel):
    id: uuid.UUID


class SendMessage(BaseModel):
    text: str


class Queued(BaseModel):
    status: Literal["queued"] = "queued"


class Feedback(BaseModel):
    rating: Literal["up", "down"]


@router.post("", status_code=status.HTTP_201_CREATED)
async def start(customer: CustomerAuth, services: Svc) -> Created:
    agent, _ = services.require_agent()
    return Created(id=await agent.start_conversation(customer.customer_id))


@router.get("")
async def mine(customer: CustomerAuth, services: Svc) -> list[ConversationSummary]:
    async with services.sessionmaker() as db:
        rows = await db.scalars(
            select(Conversation)
            .where(Conversation.customer_id == customer.customer_id)
            .order_by(Conversation.created_at.desc())
            .limit(50)
        )
        return [_summary(row) for row in rows]


@router.get("/{conversation_id}")
async def show(
    conversation_id: uuid.UUID, customer: CustomerAuth, services: Svc
) -> ConversationView:
    conversation = await owned(services, conversation_id, customer.customer_id)
    async with services.sessionmaker() as db:
        rows = list(
            await db.scalars(
                select(Message)
                .where(Message.conversation_id == conversation_id)
                .order_by(Message.seq)
            )
        )
        retracted = await retracted_seqs(services, conversation_id)
        pending = await db.scalars(
            select(Approval)
            .where(
                Approval.conversation_id == conversation_id,
                Approval.status == ApprovalStatus.PENDING,
            )
            .order_by(Approval.requested_at)
        )
        approvals = [
            PendingApproval(approval_number=a.approval_number, summary=a.summary) for a in pending
        ]
    return ConversationView(
        **_summary(conversation).model_dump(),
        rating=_rating(conversation.rating),
        messages=[m for row in rows if (m := customer_message(row, retracted)) is not None],
        pending_approvals=approvals,
    )


@router.post(
    "/{conversation_id}/messages",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(message_rate)],
    responses={413: {"description": "The message is too long."}},
)
async def send(
    conversation_id: uuid.UUID, body: SendMessage, customer: CustomerAuth, services: Svc
) -> Queued:
    text = body.text.strip()
    limit = services.settings.max_message_chars
    if len(text) > limit:
        raise HTTPException(
            status.HTTP_413_CONTENT_TOO_LARGE,
            f"Messages are limited to {limit} characters.",
        )
    if not text:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "The message is empty.")
    agent, scheduler = services.require_agent()
    conversation = await owned(services, conversation_id, customer.customer_id)
    if conversation.status is ConversationStatus.CLOSED:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "This conversation is closed. Start a new one."
        )
    try:
        await agent.store.enqueue_message(
            conversation_id, text, max_waiting=agent.limits.max_waiting_messages
        )
    except InboxFullError as error:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Please wait for the reply to your earlier messages.",
            headers={"Retry-After": "5"},
        ) from error
    scheduler.kick(conversation_id)
    return Queued()


async def owned_conversation(
    conversation_id: uuid.UUID, customer: CustomerAuth, services: Svc
) -> Conversation:
    """A dependency, so the 404 is decided before a streaming response starts."""
    return await owned(services, conversation_id, customer.customer_id)


@router.get("/{conversation_id}/events", response_class=EventSourceResponse)
async def stream_events(
    conversation: Annotated[Conversation, Depends(owned_conversation)],
    services: Svc,
    last_event_id: Annotated[str | None, Header()] = None,
    after: Annotated[int | None, Query(ge=0)] = None,
) -> AsyncIterator[ServerSentEvent]:
    """Live events; reconnect with `Last-Event-ID` (or `?after=`) to get the ones missed."""
    start = events.parse_last_event_id(last_event_id, after)
    async for event in events.stream(
        services.bus,
        services.store,
        conversation.id,
        start,
        lambda type_: type_ not in events.OPERATOR_ONLY,
    ):
        yield event


@router.post("/{conversation_id}/feedback", status_code=status.HTTP_204_NO_CONTENT)
async def feedback(
    conversation_id: uuid.UUID, body: Feedback, customer: CustomerAuth, services: Svc
) -> None:
    await owned(services, conversation_id, customer.customer_id)
    async with services.sessionmaker() as db, db.begin():
        row = await db.get_one(Conversation, conversation_id, with_for_update=True)
        row.rating = 1 if body.rating == "up" else -1


@router.post("/{conversation_id}/close", status_code=status.HTTP_204_NO_CONTENT)
async def close(conversation_id: uuid.UUID, customer: CustomerAuth, services: Svc) -> None:
    conversation = await owned(services, conversation_id, customer.customer_id)
    if conversation.status is ConversationStatus.CLOSED:
        return
    await services.store.set_status(conversation_id, ConversationStatus.CLOSED)
    event = await services.store.emit(
        conversation_id, None, "conversation.updated", {"status": "closed"}
    )
    await services.bus.publish(event)


# ---------------------------------------------------------------- helpers


async def owned(
    services: Services, conversation_id: uuid.UUID, customer_id: uuid.UUID
) -> Conversation:
    async with services.sessionmaker() as db:
        conversation = await db.get(Conversation, conversation_id)
    if conversation is None or conversation.customer_id != customer_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such conversation.")
    return conversation


async def retracted_seqs(services: Services, conversation_id: uuid.UUID) -> set[int]:
    """Assistant messages an output guard withheld: the customer sees the replacement instead."""
    async with services.sessionmaker() as db:
        rows = await db.scalars(
            select(ConversationEvent.payload["message_seq"].as_integer()).where(
                ConversationEvent.conversation_id == conversation_id,
                ConversationEvent.type == "message.retracted",
            )
        )
        return {seq for seq in rows if seq is not None}


def customer_message(row: Message, retracted: set[int]) -> ChatMessage | None:
    """What the customer sees of a transcript entry, if anything."""
    text = str(row.content.get("text") or "")
    match row.kind:
        case MessageKind.CUSTOMER:
            return ChatMessage(seq=row.seq, author="customer", text=text, created_at=row.created_at)
        case MessageKind.ASSISTANT if text and row.seq not in retracted:
            notice = "runtime" if row.content.get("written_by") == "runtime" else None
            return ChatMessage(
                seq=row.seq, author="assistant", text=text, created_at=row.created_at, notice=notice
            )
        case MessageKind.NOTICE:
            reason = str(row.content.get("reason") or "")
            author: Author = "team" if reason == OPERATOR_REPLY else "assistant"
            return ChatMessage(
                seq=row.seq, author=author, text=text, created_at=row.created_at, notice=reason
            )
        case _:
            return None


def _summary(row: Conversation) -> ConversationSummary:
    return ConversationSummary(
        id=row.id, status=row.status, created_at=row.created_at, closed_at=row.closed_at
    )


def _rating(value: int | None) -> Literal["up", "down"] | None:
    if value is None:
        return None
    return "up" if value > 0 else "down"
