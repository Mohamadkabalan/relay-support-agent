"""What a transcript is made of, before a provider renders it (PLAN.md §5.4).

The runtime appends these items; the conversation's provider renders each one exactly once into
its own wire format (`ModelProvider.render`), and that text is stored and replayed unchanged on
every later request. The model's own replies never pass through here: they are stored as the
provider returned them.
"""

import json
from dataclasses import dataclass
from typing import Any

# The user line that lets the agent speak after an operator decision, when the customer hasn't
# written anything. It carries no facts: those arrive in the trusted system note that follows it,
# so a customer who types this text gains nothing. The chat UI never shows it.
EVENT_LINE = "(automated: an update is available on your request)"


@dataclass(frozen=True, slots=True)
class CustomerText:
    text: str


@dataclass(frozen=True, slots=True)
class EventLine:
    text: str = EVENT_LINE


@dataclass(frozen=True, slots=True)
class AssistantText:
    """A reply the runtime wrote instead of the model: a handoff, an apology, a decline.

    It records in the model's transcript what the customer saw when a turn ended without a model
    reply, and it keeps the transcript valid: a system note must be followed by an assistant
    message before the next user message.
    """

    text: str


@dataclass(frozen=True, slots=True)
class SystemNote:
    """Trusted facts from the runtime, in a channel the customer can't write to."""

    text: str


@dataclass(frozen=True, slots=True)
class ToolResult:
    call_id: str
    name: str
    content: str  # exactly what the model receives
    is_error: bool


@dataclass(frozen=True, slots=True)
class ToolResults:
    """The results of one model response's tool calls, in the order the model made them."""

    results: tuple[ToolResult, ...]


Item = CustomerText | EventLine | AssistantText | SystemNote | ToolResults


def dump(payload: Any) -> str:
    """The one JSON encoding used for everything that is stored and replayed."""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def replay(raw_payloads: list[str]) -> list[Any]:
    """The provider messages for a request: every stored payload, in order, unchanged."""
    messages: list[Any] = []
    for raw in raw_payloads:
        messages.extend(json.loads(raw))
    return messages
