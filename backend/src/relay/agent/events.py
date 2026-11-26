"""What a conversation's watchers see: the events of §5.10.

Every event except a text delta is stored in `conversation_events` first, and carries that row's
ID. Clients reconnect with the last ID they saw and get everything after it (the SSE endpoint).
"""

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class AgentEvent:
    conversation_id: uuid.UUID
    type: str
    payload: dict[str, Any]
    id: int | None = None  # the stored event's ID; None for `message.delta`, which isn't stored


Listener = Callable[[AgentEvent], Awaitable[None]]
