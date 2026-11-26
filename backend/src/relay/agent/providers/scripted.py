"""A provider that plays back a script: for tests, the eval self-tests and key-free demos.

Each step is a `Reply` (what the "model" answers), a `Fail`, or a function that receives the
rendered request (a `ScriptedRequest`) and returns one of those. A test can then both check what
the loop sent and answer based on it, for example by copying a confirmation token out of the
last tool result. Payloads are rendered in the Claude format, so the replay invariants tested
with this provider are the ones the real one relies on.
"""

import itertools
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from relay.agent.pricing import cost_micros
from relay.agent.providers import anthropic
from relay.agent.providers.base import (
    EventSink,
    ModelRequest,
    ModelResponse,
    ProviderError,
    Stop,
    TextDelta,
    ToolCall,
    ToolUseStarted,
    Usage,
)
from relay.agent.transcript import Item, dump, replay

_ids = itertools.count(1)


@dataclass(frozen=True)
class Call:
    name: str
    arguments: dict[str, Any] | None = None  # None plays back unreadable arguments
    id: str = field(default_factory=lambda: f"toolu_scripted_{next(_ids):04d}")


@dataclass(frozen=True)
class Reply:
    text: str = ""
    calls: tuple[Call, ...] = ()
    stop: Stop | None = None  # default: TOOL_USE when there are calls, END_TURN otherwise
    usage: Usage = field(default_factory=lambda: Usage(input_tokens=1000, output_tokens=100))
    served_model: str | None = None
    refusal_category: str | None = None


@dataclass(frozen=True)
class Fail:
    """A step that fails the way an API error does after the SDK's retries."""

    message: str = "scripted provider error"
    infrastructure: bool = False


@dataclass
class ScriptedRequest:
    """What the loop sent: the rendered Claude-format body and the request itself."""

    request: ModelRequest
    messages: list[Any]

    def tool_results(self) -> list[dict[str, Any]]:
        """Every successful tool result in the request, parsed, oldest first."""
        found = []
        for message in self.messages:
            if message["role"] != "user" or not isinstance(message["content"], list):
                continue
            for block in message["content"]:
                if block["type"] == "tool_result" and not block["is_error"]:
                    found.append(json.loads(block["content"]))
        return found

    @property
    def last_tool_result(self) -> dict[str, Any]:
        """The newest successful tool result, for steps that react to it."""
        results = self.tool_results()
        if not results:
            raise AssertionError("no successful tool result in the request")
        return results[-1]


Step = Reply | Fail | Callable[[ScriptedRequest], Reply | Fail]


class ScriptedProvider:
    name = "scripted"

    def __init__(self, steps: list[Step], *, model: str = "scripted", effort: str | None = None):
        self.steps = list(steps)
        self.model = model
        self.effort = effort
        self.requests: list[ScriptedRequest] = []

    def render(self, item: Item) -> str:
        return anthropic.render(item)

    async def complete(self, request: ModelRequest, on_event: EventSink) -> ModelResponse:
        sent = ScriptedRequest(request, replay(list(request.transcript)))
        self.requests.append(sent)
        if not self.steps:
            raise ProviderError("the script has no more steps")
        step = self.steps.pop(0)
        if callable(step):
            step = step(sent)
        if isinstance(step, Fail):
            raise ProviderError(step.message, infrastructure=step.infrastructure)

        stop = step.stop or (Stop.TOOL_USE if step.calls else Stop.END_TURN)
        blocks: list[dict[str, Any]] = []
        if step.text:
            blocks.append({"type": "text", "text": step.text})
            if stop is not Stop.REFUSAL:
                await on_event(TextDelta(step.text))
        for call in step.calls:
            await on_event(ToolUseStarted(call.name))
            block = {"type": "tool_use", "id": call.id, "name": call.name}
            blocks.append({**block, "input": call.arguments or {}})
        appendable = stop in (Stop.END_TURN, Stop.TOOL_USE)
        served = step.served_model or self.model
        return ModelResponse(
            stop=stop,
            text=step.text,
            tool_calls=[ToolCall(call.id, call.name, call.arguments) for call in step.calls],
            usage=step.usage,
            requested_model=self.model,
            served_model=served,
            raw=dump([{"role": "assistant", "content": blocks}]) if appendable else None,
            cost_micros=cost_micros(
                served,
                input_tokens=step.usage.input_tokens,
                output_tokens=step.usage.output_tokens,
                cache_read_tokens=step.usage.cache_read_tokens,
                cache_write_tokens=step.usage.cache_write_tokens,
            ),
            latency_ms=1,
            ttft_ms=1,
            refusal_category=step.refusal_category,
        )
