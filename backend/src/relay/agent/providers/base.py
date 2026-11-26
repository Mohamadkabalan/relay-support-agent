"""The provider interface (PLAN.md §5.11): everything the loop needs from a model API.

A provider does two things:

- `render` turns a transcript item into its wire format, once, when the item is appended. The
  loop stores that text and replays it on every later request.
- `complete` sends one request, streams events to the caller, and returns a `ModelResponse`
  whose `raw` is the assistant payload to append (`None` when nothing should be appended: a
  refusal or a truncated response).
"""

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from relay.agent.manifest import ToolDef
from relay.agent.transcript import Item


class Stop(StrEnum):
    END_TURN = "end_turn"
    TOOL_USE = "tool_use"
    MAX_TOKENS = "max_tokens"
    REFUSAL = "refusal"


@dataclass(frozen=True, slots=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any] | None  # None when the model's arguments couldn't be parsed


@dataclass(frozen=True, slots=True)
class Usage:
    input_tokens: int = 0  # not served from the cache
    output_tokens: int = 0  # thinking included
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    @property
    def processed(self) -> int:
        cached = self.cache_read_tokens + self.cache_write_tokens
        return self.input_tokens + self.output_tokens + cached

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
            self.cache_read_tokens + other.cache_read_tokens,
            self.cache_write_tokens + other.cache_write_tokens,
        )


@dataclass(frozen=True, slots=True)
class ModelRequest:
    system: str
    tools: Sequence[ToolDef]
    transcript: Sequence[str]  # stored raw payloads, in order
    max_tokens: int
    user_key: str | None = None  # an opaque per-customer key for abuse monitoring, never PII


@dataclass(frozen=True, slots=True)
class ModelResponse:
    stop: Stop
    text: str
    tool_calls: list[ToolCall]
    usage: Usage
    requested_model: str
    served_model: str
    raw: str | None  # the assistant payload to append, or None
    cost_micros: int | None  # None when a model that ran has no known price
    latency_ms: int
    ttft_ms: int | None = None
    request_id: str | None = None
    refusal_category: str | None = None
    fallback_used: bool = False
    input_transformations: list[Any] = field(default_factory=list)
    thinking: str | None = None  # summarized reasoning, for the operator replay only


@dataclass(frozen=True, slots=True)
class TextDelta:
    text: str


@dataclass(frozen=True, slots=True)
class ToolUseStarted:
    name: str


ModelEvent = TextDelta | ToolUseStarted
EventSink = Callable[[ModelEvent], Awaitable[None]]


async def ignore_events(event: ModelEvent) -> None:
    return None


class ProviderError(Exception):
    """The model call failed after the SDK's own retries, or returned something unusable."""


class ModelProvider(Protocol):
    name: str
    model: str
    effort: str | None

    def render(self, item: Item) -> str: ...

    async def complete(self, request: ModelRequest, on_event: EventSink) -> ModelResponse: ...
