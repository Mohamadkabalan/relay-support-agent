"""Live conversation events over SSE (§5.10), fanned out through Redis pub/sub.

Every event except a text delta is stored first (`conversation_events`) and then published with
its ID. A stream:

1. subscribes to the conversation's channel,
2. replays the stored events after the client's `Last-Event-ID`,
3. then forwards live events, skipping any it already replayed.

Subscribing before reading the backlog means nothing falls between the two: an event committed
before the read is in the backlog, and one published after the subscription arrives live (if it
is both, the ID check drops the copy). Text deltas carry no ID, so a reconnect resumes after the
last stored event and the client gets the full message from `message.completed`.

FastAPI's SSE support sends a `: ping` comment after 15 idle seconds, the heartbeat that keeps
proxies from closing the stream.
"""

import json
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi.sse import ServerSentEvent
from redis.asyncio import Redis
from redis.asyncio.client import PubSub

from relay.agent.events import AgentEvent
from relay.agent.store import ConversationStore

BACKLOG_PAGE = 500
RETRY_MS = 3000

# Console-only events: the customer's stream leaves them out.
OPERATOR_ONLY = frozenset({"guard.input"})


class EventBus:
    def __init__(self, redis: Redis, prefix: str = "relay:") -> None:
        self.redis = redis
        self.prefix = prefix

    def channel(self, conversation_id: uuid.UUID) -> str:
        return f"{self.prefix}events:{conversation_id}"

    async def publish(self, event: AgentEvent) -> None:
        message = {"id": event.id, "type": event.type, "payload": event.payload}
        await self.redis.publish(self.channel(event.conversation_id), json.dumps(message))

    @asynccontextmanager
    async def subscribe(
        self, conversation_id: uuid.UUID
    ) -> AsyncIterator[AsyncIterator[AgentEvent]]:
        pubsub = self.redis.pubsub()
        channel = self.channel(conversation_id)
        await pubsub.subscribe(channel)
        try:
            await _confirmed(pubsub)
            yield _listen(pubsub, conversation_id)
        finally:
            await pubsub.unsubscribe(channel)
            await pubsub.aclose()  # type: ignore[no-untyped-call]  # redis-py leaves it untyped


async def _confirmed(pubsub: PubSub) -> None:
    """Wait until Redis has registered the subscription."""
    while True:
        message = await pubsub.get_message(timeout=5.0)
        if message is None:
            raise TimeoutError("Redis did not confirm the subscription")
        if message["type"] == "subscribe":
            return


async def _listen(pubsub: PubSub, conversation_id: uuid.UUID) -> AsyncIterator[AgentEvent]:
    async for message in pubsub.listen():
        if message["type"] != "message":
            continue
        data = json.loads(message["data"])
        yield AgentEvent(conversation_id, data["type"], data["payload"], data["id"])


def to_sse(type_: str, payload: dict[str, Any], event_id: int | None) -> ServerSentEvent:
    return ServerSentEvent(
        event=type_, data=payload, id=str(event_id) if event_id is not None else None
    )


async def stream(
    bus: EventBus,
    store: ConversationStore,
    conversation_id: uuid.UUID,
    last_event_id: int,
    visible: Callable[[str], bool],
) -> AsyncIterator[ServerSentEvent]:
    async with bus.subscribe(conversation_id) as live:
        yield ServerSentEvent(comment="connected", retry=RETRY_MS)
        replayed: set[int] = set()
        after = last_event_id
        while True:
            rows = await store.events(conversation_id, after=after, limit=BACKLOG_PAGE)
            for row in rows:
                replayed.add(row.id)
                after = row.id
                if visible(row.type):
                    yield to_sse(row.type, row.payload, row.id)
            if len(rows) < BACKLOG_PAGE:
                break
        async for event in live:
            if event.id is not None and (event.id in replayed or event.id <= last_event_id):
                continue
            if visible(event.type):
                yield to_sse(event.type, event.payload, event.id)


def parse_last_event_id(header: str | None, query: int | None) -> int:
    """`Last-Event-ID` from a reconnecting EventSource, or `?after=` for a first connection."""
    for value in (header, query):
        if value is None:
            continue
        try:
            number = int(value)
        except ValueError:
            continue
        if number >= 0:
            return number
    return 0
