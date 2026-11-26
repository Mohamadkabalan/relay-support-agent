"""The conversation store: the append-only transcript and the record of every turn (ADR-0005).

Each method runs in its own short transaction. The loop calls them in order under the
conversation's single-flight lock, so sequence numbers are assigned without races (unique
constraints back that up).

What is pinned on the conversation when it starts (provider, model, effort, system prompt text,
tool manifest text, policy version) is never changed afterwards. Transcript entries are only ever
inserted, and a trigger rejects UPDATE and DELETE on `messages`.
"""

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from relay.agent.events import AgentEvent
from relay.agent.providers.base import ModelResponse
from relay.agent.tools import ExecutedCall
from relay.clock import Clock
from relay.db.models import (
    Conversation,
    ConversationEvent,
    Customer,
    InboxItem,
    Message,
    ModelCall,
    Order,
    ToolCall,
    Turn,
)
from relay.domain.enums import (
    ConversationStatus,
    EscalationPriority,
    EscalationReason,
    InboxKind,
    MessageKind,
    TurnStatus,
    TurnTrigger,
)
from relay.domain.escalations import open_escalation
from relay.domain.policy_config import PolicyConfig
from relay.domain.results import ApprovalView, EscalationResult
from relay.domain.scope import Deps, SystemScope

RUNTIME_JOB = "agent-runtime"  # the actor recorded when the runtime itself escalates

# A message an operator typed into an escalated conversation: a notice with this reason.
OPERATOR_REPLY = "operator"


class InboxFullError(Exception):
    """Too many customer messages are already waiting for the running turn."""


@dataclass(frozen=True, slots=True)
class Inbound:
    kind: InboxKind
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class StartedTurn:
    id: uuid.UUID
    seq: int
    trigger: TurnTrigger
    inputs: list[Inbound]


@dataclass(frozen=True, slots=True)
class Pinned:
    """What a conversation was started with."""

    provider: str
    model: str
    effort: str | None
    prompt_version: str
    prompt_hash: str
    system_prompt: str
    tool_manifest: str
    manifest_hash: str
    policy_version: str


class ConversationStore:
    def __init__(
        self, sessionmaker: async_sessionmaker[AsyncSession], clock: Clock, policy: PolicyConfig
    ) -> None:
        self.sessionmaker = sessionmaker
        self.clock = clock
        self.policy = policy

    # ------------------------------------------------------------ conversations

    async def create(
        self, customer_id: uuid.UUID, pinned: Pinned, conversation_id: uuid.UUID | None = None
    ) -> uuid.UUID:
        async with self.sessionmaker() as session, session.begin():
            conversation = Conversation(
                id=conversation_id or uuid.uuid7(),
                customer_id=customer_id,
                status=ConversationStatus.ACTIVE,
                provider=pinned.provider,
                model=pinned.model,
                effort=pinned.effort,
                prompt_version=pinned.prompt_version,
                prompt_hash=pinned.prompt_hash,
                system_prompt=pinned.system_prompt,
                tool_manifest=pinned.tool_manifest,
                manifest_hash=pinned.manifest_hash,
                policy_version=pinned.policy_version,
                input_tokens=0,
                output_tokens=0,
                cache_read_tokens=0,
                cache_write_tokens=0,
                cost_micros=0,
                cost_complete=True,
                injection_flags=0,
                budget_warned=False,
                flags=[],
                created_at=self.clock.now(),
            )
            session.add(conversation)
        return conversation.id

    async def get(self, conversation_id: uuid.UUID) -> Conversation:
        async with self.sessionmaker() as session:
            return await session.get_one(Conversation, conversation_id)

    async def set_status(self, conversation_id: uuid.UUID, status: ConversationStatus) -> None:
        values: dict[str, Any] = {"status": status}
        if status is ConversationStatus.CLOSED:
            values["closed_at"] = self.clock.now()
        await self._update(conversation_id, **values)

    async def flag(self, conversation_id: uuid.UUID, flag: dict[str, Any]) -> None:
        """Record something for review (a retraction, a refusal, a failure)."""
        async with self.sessionmaker() as session, session.begin():
            conversation = await session.get_one(
                Conversation, conversation_id, with_for_update=True
            )
            conversation.flags = [*conversation.flags, flag]

    async def count_injection_flag(self, conversation_id: uuid.UUID) -> int:
        async with self.sessionmaker() as session, session.begin():
            count = await session.scalar(
                update(Conversation)
                .where(Conversation.id == conversation_id)
                .values(injection_flags=Conversation.injection_flags + 1)
                .returning(Conversation.injection_flags)
            )
        return int(count or 0)

    async def mark_budget_warned(self, conversation_id: uuid.UUID) -> None:
        await self._update(conversation_id, budget_warned=True)

    async def escalate(
        self,
        conversation_id: uuid.UUID,
        *,
        reason: EscalationReason,
        summary: str,
        priority: EscalationPriority = EscalationPriority.NORMAL,
    ) -> EscalationResult:
        """Open an escalation on the customer's behalf and hand the conversation to a human."""
        async with self.sessionmaker() as session, session.begin():
            conversation = await session.get_one(
                Conversation, conversation_id, with_for_update=True
            )
            result = await open_escalation(
                Deps(session=session, clock=self.clock, policy=self.policy),
                SystemScope(job=RUNTIME_JOB),
                customer_id=conversation.customer_id,
                conversation_id=conversation_id,
                reason=reason,
                summary=summary,
                priority=priority,
            )
            conversation.status = ConversationStatus.ESCALATED
        return result

    # ------------------------------------------------------------ inbox and turns

    async def enqueue_message(
        self, conversation_id: uuid.UUID, text: str, *, max_waiting: int = 3
    ) -> None:
        async with self.sessionmaker() as session, session.begin():
            waiting = await session.scalar(
                select(func.count())
                .select_from(InboxItem)
                .where(
                    InboxItem.conversation_id == conversation_id,
                    InboxItem.turn_id.is_(None),
                    InboxItem.kind == InboxKind.CUSTOMER_MESSAGE,
                )
            )
            if (waiting or 0) >= max_waiting:
                raise InboxFullError(f"{waiting} messages are already waiting")
            session.add(
                InboxItem(
                    conversation_id=conversation_id,
                    kind=InboxKind.CUSTOMER_MESSAGE,
                    payload={"text": text},
                    created_at=self.clock.now(),
                )
            )

    async def enqueue_approval(
        self,
        conversation_id: uuid.UUID,
        approval: ApprovalView,
        session: AsyncSession | None = None,
    ) -> AgentEvent:
        """An operator decided: queue the outcome for a resume turn (§5.9).

        Pass the session that resolved the approval, so the decision and the resume it schedules
        commit together. Returns the stored `approval.resolved` event, to publish after commit.
        """
        async with self._transaction(session) as db:
            db.add(
                InboxItem(
                    conversation_id=conversation_id,
                    kind=InboxKind.APPROVAL_RESOLVED,
                    payload=approval.model_dump(mode="json"),
                    created_at=self.clock.now(),
                )
            )
            event = self._event(
                conversation_id,
                None,
                "approval.resolved",
                {"approval_number": approval.approval_number, "decision": approval.status.value},
            )
            db.add(event)
            await db.flush()
        return AgentEvent(conversation_id, event.type, event.payload, event.id)

    async def has_waiting(self, conversation_id: uuid.UUID) -> bool:
        """Whether anything is in the inbox that no turn has claimed yet."""
        async with self.sessionmaker() as session:
            found = await session.scalar(
                select(InboxItem.id)
                .where(InboxItem.conversation_id == conversation_id, InboxItem.turn_id.is_(None))
                .limit(1)
            )
        return found is not None

    async def start_turn(self, conversation_id: uuid.UUID) -> StartedTurn | None:
        """Claim everything waiting in the inbox and open a turn for it. None if it was empty."""
        async with self.sessionmaker() as session, session.begin():
            await session.get_one(Conversation, conversation_id, with_for_update=True)
            waiting = list(
                await session.scalars(
                    select(InboxItem)
                    .where(
                        InboxItem.conversation_id == conversation_id, InboxItem.turn_id.is_(None)
                    )
                    .order_by(InboxItem.id)
                    .with_for_update()
                )
            )
            if not waiting:
                return None
            kinds = {item.kind for item in waiting}
            trigger = (
                TurnTrigger.CUSTOMER_MESSAGE
                if InboxKind.CUSTOMER_MESSAGE in kinds
                else TurnTrigger.APPROVAL_RESOLVED
            )
            last = await session.scalar(
                select(func.max(Turn.seq)).where(Turn.conversation_id == conversation_id)
            )
            turn = Turn(
                conversation_id=conversation_id,
                seq=(last or 0) + 1,
                trigger=trigger,
                status=TurnStatus.RUNNING,
                steps=0,
                tool_call_count=0,
                input_tokens=0,
                output_tokens=0,
                cache_read_tokens=0,
                cache_write_tokens=0,
                cost_micros=0,
                retracted=False,
                started_at=self.clock.now(),
            )
            session.add(turn)
            await session.flush()
            for item in waiting:
                item.turn_id = turn.id
            return StartedTurn(
                turn.id, turn.seq, trigger, [Inbound(item.kind, item.payload) for item in waiting]
            )

    async def finish_turn(
        self,
        turn_id: uuid.UUID,
        status: TurnStatus,
        *,
        latency_ms: int,
        error: str | None = None,
        retracted: bool = False,
        trace_id: str | None = None,
    ) -> None:
        async with self.sessionmaker() as session, session.begin():
            await session.execute(
                update(Turn)
                .where(Turn.id == turn_id)
                .values(
                    status=status,
                    latency_ms=latency_ms,
                    error=error,
                    retracted=retracted,
                    trace_id=trace_id,
                    finished_at=self.clock.now(),
                )
            )

    async def running_turns(self) -> list[Turn]:
        """Turns not finished yet: running now, or orphaned by a process that stopped."""
        async with self.sessionmaker() as session:
            rows = await session.scalars(
                select(Turn).where(Turn.status == TurnStatus.RUNNING).order_by(Turn.started_at)
            )
            return list(rows)

    async def release_inbox(self, turn_id: uuid.UUID) -> int:
        """Give a turn's claimed inputs back to the inbox, for a turn that never used them."""
        async with self.sessionmaker() as session, session.begin():
            result = await session.execute(
                update(InboxItem).where(InboxItem.turn_id == turn_id).values(turn_id=None)
            )
        return int(getattr(result, "rowcount", 0) or 0)

    async def last_turn(self, conversation_id: uuid.UUID) -> Turn | None:
        async with self.sessionmaker() as session:
            return await session.scalar(
                select(Turn)
                .where(Turn.conversation_id == conversation_id)
                .order_by(Turn.seq.desc())
            )

    async def turns_before(self, conversation_id: uuid.UUID, seq: int) -> list[Turn]:
        async with self.sessionmaker() as session:
            rows = await session.scalars(
                select(Turn)
                .where(Turn.conversation_id == conversation_id, Turn.seq < seq)
                .order_by(Turn.seq)
            )
            return list(rows)

    # ------------------------------------------------------------ transcript

    async def append(
        self,
        conversation_id: uuid.UUID,
        turn_id: uuid.UUID | None,
        kind: MessageKind,
        content: dict[str, Any],
        raw: str | None,
    ) -> int:
        """Add one transcript entry and return its sequence number."""
        async with self.sessionmaker() as session, session.begin():
            last = await session.scalar(
                select(func.max(Message.seq)).where(Message.conversation_id == conversation_id)
            )
            seq = (last or 0) + 1
            session.add(
                Message(
                    conversation_id=conversation_id,
                    seq=seq,
                    turn_id=turn_id,
                    kind=kind,
                    content=content,
                    raw=raw,
                    created_at=self.clock.now(),
                )
            )
        return seq

    async def transcript(self, conversation_id: uuid.UUID) -> list[str]:
        """Every stored payload the model sees, in order (notices have none)."""
        async with self.sessionmaker() as session:
            rows = await session.scalars(
                select(Message.raw)
                .where(Message.conversation_id == conversation_id, Message.raw.is_not(None))
                .order_by(Message.seq)
            )
            return [raw for raw in rows if raw is not None]

    async def messages(self, conversation_id: uuid.UUID) -> list[Message]:
        async with self.sessionmaker() as session:
            rows = await session.scalars(
                select(Message)
                .where(Message.conversation_id == conversation_id)
                .order_by(Message.seq)
            )
            return list(rows)

    async def turn_messages(self, turn_id: uuid.UUID) -> list[Message]:
        async with self.sessionmaker() as session:
            rows = await session.scalars(
                select(Message).where(Message.turn_id == turn_id).order_by(Message.seq)
            )
            return list(rows)

    async def last_model_message(self, conversation_id: uuid.UUID) -> Message | None:
        """The latest entry the model sees (notices have no payload and don't count)."""
        async with self.sessionmaker() as session:
            return await session.scalar(
                select(Message)
                .where(Message.conversation_id == conversation_id, Message.raw.is_not(None))
                .order_by(Message.seq.desc())
                .limit(1)
            )

    async def operator_replies_since_last_turn(self, conversation_id: uuid.UUID) -> list[str]:
        """What operators told the customer since the last turn, so the next turn can relay it.

        Operator replies are notices written outside any turn; everything a turn appends has its
        turn ID. Each reply is therefore relayed exactly once, by the first turn after it.
        """
        async with self.sessionmaker() as session:
            last_turn_seq = await session.scalar(
                select(func.max(Message.seq)).where(
                    Message.conversation_id == conversation_id, Message.turn_id.is_not(None)
                )
            )
            rows = await session.scalars(
                select(Message.content)
                .where(
                    Message.conversation_id == conversation_id,
                    Message.kind == MessageKind.NOTICE,
                    Message.turn_id.is_(None),
                    Message.seq > (last_turn_seq or 0),
                )
                .order_by(Message.seq)
            )
            return [
                str(content.get("text", ""))
                for content in rows
                if content.get("reason") == OPERATOR_REPLY
            ]

    async def customer_texts(self, conversation_id: uuid.UUID) -> list[str]:
        async with self.sessionmaker() as session:
            rows = await session.scalars(
                select(Message.content).where(
                    Message.conversation_id == conversation_id,
                    Message.kind == MessageKind.CUSTOMER,
                )
            )
            return [str(content.get("text", "")) for content in rows]

    # ------------------------------------------------------------ calls and events

    async def record_model_call(
        self,
        conversation_id: uuid.UUID,
        turn_id: uuid.UUID,
        seq: int,
        *,
        provider: str,
        effort: str | None,
        max_tokens: int,
        response: ModelResponse,
    ) -> None:
        usage = response.usage
        cost = response.cost_micros
        async with self.sessionmaker() as session, session.begin():
            session.add(
                ModelCall(
                    turn_id=turn_id,
                    seq=seq,
                    provider=provider,
                    requested_model=response.requested_model,
                    served_model=response.served_model,
                    effort=effort,
                    max_tokens=max_tokens,
                    stop_reason=response.stop.value,
                    refusal_category=response.refusal_category,
                    fallback_used=response.fallback_used,
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    cache_read_tokens=usage.cache_read_tokens,
                    cache_write_tokens=usage.cache_write_tokens,
                    cost_micros=cost,
                    latency_ms=response.latency_ms,
                    ttft_ms=response.ttft_ms,
                    request_id=response.request_id,
                    input_transformations=response.input_transformations or None,
                    thinking=response.thinking,
                    created_at=self.clock.now(),
                )
            )
            totals = {
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "cache_read_tokens": usage.cache_read_tokens,
                "cache_write_tokens": usage.cache_write_tokens,
                "cost_micros": cost or 0,
            }
            await session.execute(
                update(Turn)
                .where(Turn.id == turn_id)
                .values(
                    steps=Turn.steps + 1,
                    ttft_ms=func.coalesce(Turn.ttft_ms, response.ttft_ms),
                    **{name: getattr(Turn, name) + value for name, value in totals.items()},
                )
            )
            conversation_values: dict[str, Any] = {
                name: getattr(Conversation, name) + value for name, value in totals.items()
            }
            if cost is None:
                conversation_values["cost_complete"] = False
            await session.execute(
                update(Conversation)
                .where(Conversation.id == conversation_id)
                .values(**conversation_values)
            )

    async def record_tool_calls(
        self, turn_id: uuid.UUID, first_seq: int, executed: list[ExecutedCall]
    ) -> None:
        async with self.sessionmaker() as session, session.begin():
            for offset, call in enumerate(executed):
                session.add(
                    ToolCall(
                        turn_id=turn_id,
                        seq=first_seq + offset,
                        tool_use_id=call.call.id,
                        name=call.call.name,
                        arguments=call.call.arguments,
                        result=call.result.content,
                        is_error=call.result.is_error,
                        outcome=call.outcome,
                        error_code=call.error_code,
                        latency_ms=call.latency_ms,
                        attempts=call.attempts,
                        created_at=self.clock.now(),
                    )
                )
            await session.execute(
                update(Turn)
                .where(Turn.id == turn_id)
                .values(tool_call_count=Turn.tool_call_count + len(executed))
            )

    async def emit(
        self,
        conversation_id: uuid.UUID,
        turn_id: uuid.UUID | None,
        type_: str,
        payload: dict[str, Any],
        session: AsyncSession | None = None,
    ) -> AgentEvent:
        """Store an event and return it with its ID, ready to publish."""
        async with self._transaction(session) as db:
            event = self._event(conversation_id, turn_id, type_, payload)
            db.add(event)
            await db.flush()
        return AgentEvent(conversation_id, type_, payload, event.id)

    async def events(
        self, conversation_id: uuid.UUID, after: int = 0, limit: int | None = None
    ) -> list[ConversationEvent]:
        """Stored events in order, optionally only those after a given event ID."""
        async with self.sessionmaker() as session:
            query = (
                select(ConversationEvent)
                .where(
                    ConversationEvent.conversation_id == conversation_id,
                    ConversationEvent.id > after,
                )
                .order_by(ConversationEvent.id)
            )
            if limit is not None:
                query = query.limit(limit)
            return list(await session.scalars(query))

    def _event(
        self,
        conversation_id: uuid.UUID,
        turn_id: uuid.UUID | None,
        type_: str,
        payload: dict[str, Any],
    ) -> ConversationEvent:
        return ConversationEvent(
            conversation_id=conversation_id,
            turn_id=turn_id,
            type=type_,
            payload=payload,
            created_at=self.clock.now(),
        )

    # ------------------------------------------------------------ the customer

    async def customer_name(self, customer_id: uuid.UUID) -> str:
        async with self.sessionmaker() as session:
            return (await session.get_one(Customer, customer_id)).full_name

    async def order_numbers(self, customer_id: uuid.UUID) -> set[str]:
        async with self.sessionmaker() as session:
            rows = await session.scalars(
                select(Order.number).where(Order.customer_id == customer_id)
            )
            return set(rows)

    @asynccontextmanager
    async def _transaction(self, session: AsyncSession | None) -> AsyncIterator[AsyncSession]:
        """The caller's session (its transaction commits the work), or a new transaction."""
        if session is not None:
            yield session
            return
        async with self.sessionmaker() as own, own.begin():
            yield own

    async def _update(self, conversation_id: uuid.UUID, **values: Any) -> None:
        async with self.sessionmaker() as session, session.begin():
            await session.execute(
                update(Conversation).where(Conversation.id == conversation_id).values(**values)
            )
