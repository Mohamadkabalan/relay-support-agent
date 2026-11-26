"""Driving the agent loop in tests, and the transcript invariants every request must satisfy."""

import itertools
import json
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from relay.agent.loop import Agent, AgentEvent, TurnOutcome
from relay.agent.providers.scripted import ScriptedProvider, ScriptedRequest
from relay.db.models import Message, ToolCall, Turn
from relay.domain.enums import MessageKind


@dataclass
class EventLog:
    events: list[AgentEvent] = field(default_factory=list)

    async def __call__(self, event: AgentEvent) -> None:
        self.events.append(event)

    def types(self) -> list[str]:
        return [event.type for event in self.events if event.type != "message.delta"]

    def of(self, type_: str) -> list[dict[str, Any]]:
        return [event.payload for event in self.events if event.type == type_]


@dataclass
class Harness:
    agent: Agent
    provider: ScriptedProvider
    log: EventLog
    session: AsyncSession  # the test's own session, for checking rows

    async def start(self, customer_id: uuid.UUID) -> uuid.UUID:
        return await self.agent.start_conversation(customer_id)

    async def say(self, conversation_id: uuid.UUID, text: str) -> TurnOutcome:
        outcome = await self.agent.send(conversation_id, text)
        assert outcome is not None
        return outcome

    async def kinds(self, conversation_id: uuid.UUID) -> list[MessageKind]:
        rows = await self.session.scalars(
            select(Message.kind)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.seq)
        )
        return list(rows)

    async def tool_calls(self, conversation_id: uuid.UUID) -> list[ToolCall]:
        rows = await self.session.scalars(
            select(ToolCall)
            .join(Turn)
            .where(Turn.conversation_id == conversation_id)
            .order_by(Turn.seq, ToolCall.seq)
        )
        return list(rows)

    def check_requests(self) -> None:
        check_requests(self.provider.requests)


def check_requests(requests: list[ScriptedRequest]) -> None:
    """Every request is a valid Claude transcript, and extends the previous one unchanged."""
    for sent in requests:
        assert_valid_transcript(sent.messages)
    for before, after in itertools.pairwise(requests):
        prefix = before.messages
        assert json.dumps(after.messages[: len(prefix)]) == json.dumps(prefix), "history changed"
        assert after.request.system == before.request.system
        assert after.request.tools == before.request.tools


def assert_valid_transcript(messages: list[dict[str, Any]]) -> None:
    """The ordering rules the Messages API enforces, checked locally."""
    assert messages
    assert messages[0]["role"] == "user"
    for i, message in enumerate(messages):
        role = message["role"]
        if role == "system":
            assert messages[i - 1]["role"] == "user", f"system message {i} doesn't follow a user"
            assert i == len(messages) - 1 or messages[i + 1]["role"] == "assistant", (
                f"system message {i} is followed by {messages[i + 1]['role']}"
            )
        if role == "assistant":
            wanted = [b["id"] for b in message["content"] if b["type"] == "tool_use"]
            if wanted:
                answer = messages[i + 1]
                given = [b["tool_use_id"] for b in answer["content"] if b["type"] == "tool_result"]
                assert answer["role"] == "user", f"no tool results after message {i}"
                assert given == wanted, f"tool results don't match the calls of message {i}"
