"""The turn scheduler (ADR-0007): turns run as asyncio tasks in the API process.

`kick(conversation_id)` is called whenever something lands in a conversation's inbox: a customer
message, or an approval outcome. It starts a task that runs turns until the inbox is empty.

The single-flight lock makes this safe with any number of tasks and API replicas:

- A task that can't take the lock stops at once. The holder will see the new input, because it
  checks the inbox again *after* releasing the lock, and the new input was committed before the
  failed attempt to lock. No wake-up is lost, and no conversation ever runs two turns at once.
- After each turn the task loops; it stops only when a turn found nothing to do and the inbox is
  still empty once the lock is released.

A periodic sweep does the work no request triggers:

- **Orphaned turns.** A turn still `running` whose lock is free belongs to a process that stopped.
  `Agent.recover` finishes it so the conversation can continue.
- **Approval expiry.** Approvals undecided for 24 hours expire and open an escalation (§5.3); the
  conversation gets a resume turn so the customer hears about it.
- **Idle close.** An active conversation with no activity for 30 minutes and nothing pending is
  closed.
"""

import asyncio
import contextlib
import logging
import uuid
from datetime import timedelta

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from relay.agent.events import AgentEvent, Listener
from relay.agent.loop import Agent, ConversationClosedError, TurnInProgressError
from relay.db.models import Approval, Conversation, InboxItem, Message, Turn
from relay.domain import approvals
from relay.domain.enums import ApprovalStatus, ConversationStatus, TurnStatus
from relay.domain.scope import Deps, SystemScope

logger = logging.getLogger(__name__)

EXPIRY_JOB = "approval-expiry"


class Scheduler:
    def __init__(
        self,
        agent: Agent,
        sessionmaker: async_sessionmaker[AsyncSession],
        *,
        publish: Listener | None = None,
        idle_after: timedelta = timedelta(minutes=30),
    ) -> None:
        self.agent = agent
        self.sessionmaker = sessionmaker
        self.publish = publish
        self.idle_after = idle_after
        self.tasks: set[asyncio.Task[None]] = set()
        self._sweeper: asyncio.Task[None] | None = None

    # ------------------------------------------------------------ turns

    def kick(self, conversation_id: uuid.UUID) -> asyncio.Task[None]:
        """Run turns for this conversation until its inbox is empty, in the background."""
        task = asyncio.create_task(self.drain(conversation_id), name=f"turns:{conversation_id}")
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    async def drain(self, conversation_id: uuid.UUID) -> None:
        store = self.agent.store
        while True:
            try:
                outcome = await self.agent.run_turn(conversation_id)
            except TurnInProgressError:
                return  # the holder checks the inbox again after it unlocks
            except ConversationClosedError:
                return
            except Exception:
                logger.exception("Running a turn for conversation %s failed", conversation_id)
                return
            if outcome is None and not await store.has_waiting(conversation_id):
                return

    async def idle(self) -> None:
        """Wait until every turn task has finished (tests, and a graceful shutdown)."""
        while self.tasks:
            await asyncio.gather(*list(self.tasks), return_exceptions=True)

    # ------------------------------------------------------------ the sweep

    async def sweep(self) -> None:
        for job in (self.recover_orphans, self.expire_approvals, self.close_idle):
            try:
                await job()
            except Exception:
                logger.exception("Sweep job %s failed", job.__name__)

    async def recover_orphans(self) -> list[uuid.UUID]:
        recovered = []
        for turn in await self.agent.store.running_turns():
            outcome = await self.agent.recover(turn)
            if outcome is None:
                continue
            logger.warning(
                "Recovered turn %s of conversation %s as %s",
                turn.seq,
                turn.conversation_id,
                outcome.status.value,
            )
            recovered.append(turn.conversation_id)
            if await self.agent.store.has_waiting(turn.conversation_id):
                self.kick(turn.conversation_id)
        return recovered

    async def expire_approvals(self) -> list[str]:
        """Expire overdue approvals; each conversation gets the outcome and a resume turn."""
        store = self.agent.store
        events: list[AgentEvent] = []
        async with self.sessionmaker() as session, session.begin():
            deps = Deps(session=session, clock=self.agent.clock, policy=self.agent.policy)
            numbers = await approvals.expire_due(deps, SystemScope(job=EXPIRY_JOB))
            for number in numbers:
                conversation_id = await session.scalar(
                    select(Approval.conversation_id).where(Approval.approval_number == number)
                )
                if conversation_id is None:
                    continue  # made over stdio: there is no conversation to tell
                view = await approvals.get_approval(deps, number)
                events.append(await store.enqueue_approval(conversation_id, view, session))
        for event in events:
            await self._publish(event)
            self.kick(event.conversation_id)
        return numbers

    async def close_idle(self) -> list[uuid.UUID]:
        """Close active conversations idle for `idle_after`, unless something is pending."""
        cutoff = self.agent.clock.now() - self.idle_after
        last_message = (
            select(func.max(Message.created_at))
            .where(Message.conversation_id == Conversation.id)
            .scalar_subquery()
        )
        pending = select(Approval.id).where(
            Approval.conversation_id == Conversation.id,
            Approval.status == ApprovalStatus.PENDING,
        )
        running = select(Turn.id).where(
            Turn.conversation_id == Conversation.id, Turn.status == TurnStatus.RUNNING
        )
        waiting = select(InboxItem.id).where(
            InboxItem.conversation_id == Conversation.id, InboxItem.turn_id.is_(None)
        )
        async with self.sessionmaker() as session, session.begin():
            closed = list(
                await session.scalars(
                    update(Conversation)
                    .where(
                        Conversation.status == ConversationStatus.ACTIVE,
                        func.coalesce(last_message, Conversation.created_at) < cutoff,
                        ~pending.exists(),
                        ~running.exists(),
                        ~waiting.exists(),
                    )
                    .values(status=ConversationStatus.CLOSED, closed_at=self.agent.clock.now())
                    .returning(Conversation.id)
                )
            )
        for conversation_id in closed:
            event = await self.agent.store.emit(
                conversation_id, None, "conversation.updated", {"status": "closed"}
            )
            await self._publish(event)
        return closed

    def start_sweeping(self, every_s: float = 60.0) -> None:
        async def loop() -> None:
            while True:
                await self.sweep()
                await asyncio.sleep(every_s)

        self._sweeper = asyncio.create_task(loop(), name="scheduler-sweep")

    async def stop(self, grace_s: float = 10.0) -> None:
        """Stop sweeping, give running turns `grace_s` to finish, then cancel the rest.

        A cancelled turn stays `running`; the next process's sweep recovers it.
        """
        if self._sweeper is not None:
            self._sweeper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._sweeper
        if self.tasks:
            _, unfinished = await asyncio.wait(list(self.tasks), timeout=grace_s)
            for task in unfinished:
                task.cancel()
            await asyncio.gather(*unfinished, return_exceptions=True)

    async def _publish(self, event: AgentEvent) -> None:
        if self.publish is not None:
            await self.publish(event)
