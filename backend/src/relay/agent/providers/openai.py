"""An OpenAI provider on the Responses API, behind the same interface (§5.11).

It exists to show the loop isn't tied to one vendor. It passes the provider contract tests
against a fake transport; it has not been run against the live API in this repository, and the
published numbers never use it.

Mapping:

- Trusted system notes become `developer` messages; tool results become `function_call_output`
  items. The tools are the same pinned manifest, as strict function tools.
- Requests are stateless (`store: false`): every request replays the full transcript, and
  reasoning items come back encrypted (`include: ["reasoning.encrypted_content"]`) so they can be
  replayed as returned.
- The call is not streamed: the reply's text is emitted as one event when the response arrives.
- OpenAI counts cached tokens inside `input_tokens`; `Usage.input_tokens` here is the uncached
  part, as for Claude. There is no price table for OpenAI models, so cost is `None`.
"""

import json
import time
from typing import Any

from openai import APIError, AsyncOpenAI
from pydantic import BaseModel

from relay.agent.manifest import ToolDef
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
from relay.agent.transcript import (
    AssistantText,
    CustomerText,
    EventLine,
    Item,
    SystemNote,
    ToolResults,
    dump,
    replay,
)


def render(item: Item) -> str:
    match item:
        case CustomerText(text) | EventLine(text):
            return dump([{"role": "user", "content": text}])
        case AssistantText(text):
            return dump([{"role": "assistant", "content": text}])
        case SystemNote(text):
            return dump([{"role": "developer", "content": text}])
        case ToolResults(results):
            outputs = [
                {"type": "function_call_output", "call_id": r.call_id, "output": r.content}
                for r in results
            ]
            return dump(outputs)


def render_tools(tools: list[ToolDef]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.input_schema,
            "strict": True,
        }
        for tool in sorted(tools, key=lambda tool: tool.name)
    ]


def echo_form(item: BaseModel) -> dict[str, Any]:
    """An output item the way the SDK itself sends it back as input."""
    return item.model_dump(
        mode="json", exclude_unset=True, exclude=getattr(item, "__api_exclude__", None)
    )


class OpenAIProvider:
    name = "openai"

    def __init__(self, client: AsyncOpenAI, *, model: str, effort: str | None = "medium") -> None:
        self.client = client
        self.model = model
        self.effort = effort

    def render(self, item: Item) -> str:
        return render(item)

    def build_params(self, request: ModelRequest) -> dict[str, Any]:
        params: dict[str, Any] = {
            "model": self.model,
            "instructions": request.system,
            "input": replay(list(request.transcript)),
            "tools": render_tools(list(request.tools)),
            "tool_choice": "auto",
            "parallel_tool_calls": True,
            "max_output_tokens": request.max_tokens,
            "store": False,
            "include": ["reasoning.encrypted_content"],
        }
        if self.effort:
            params["reasoning"] = {"effort": self.effort}
        if request.user_key:
            params["safety_identifier"] = request.user_key
        return params

    async def complete(self, request: ModelRequest, on_event: EventSink) -> ModelResponse:
        started = time.monotonic()
        try:
            response = await self.client.responses.create(**self.build_params(request))
        except APIError as error:
            raise ProviderError(f"OpenAI API error: {error}") from error

        items = [echo_form(item) for item in response.output]
        text_parts: list[str] = []
        refused = False
        calls: list[ToolCall] = []
        for item in items:
            if item["type"] == "message":
                for part in item.get("content", []):
                    if part["type"] == "output_text":
                        text_parts.append(part["text"])
                    elif part["type"] == "refusal":
                        refused = True
            elif item["type"] == "function_call":
                calls.append(ToolCall(item["call_id"], item["name"], _arguments(item["arguments"])))
                await on_event(ToolUseStarted(item["name"]))
        text = "".join(text_parts)

        incomplete = response.incomplete_details.reason if response.incomplete_details else None
        if refused or incomplete == "content_filter":
            stop = Stop.REFUSAL
        elif incomplete == "max_output_tokens":
            stop = Stop.MAX_TOKENS
        elif response.status != "completed":
            raise ProviderError(f"OpenAI response status {response.status!r}")
        elif calls:
            stop = Stop.TOOL_USE
        else:
            stop = Stop.END_TURN
        if text and stop is not Stop.REFUSAL:
            await on_event(TextDelta(text))

        usage = Usage()
        if response.usage is not None:
            cached = response.usage.input_tokens_details.cached_tokens or 0
            usage = Usage(
                input_tokens=response.usage.input_tokens - cached,
                output_tokens=response.usage.output_tokens,
                cache_read_tokens=cached,
            )
        appendable = stop in (Stop.END_TURN, Stop.TOOL_USE) and bool(items)
        return ModelResponse(
            stop=stop,
            text=text,
            tool_calls=calls,
            usage=usage,
            requested_model=self.model,
            served_model=response.model,
            raw=dump(items) if appendable else None,
            cost_micros=None,
            latency_ms=int((time.monotonic() - started) * 1000),
            request_id=getattr(response, "_request_id", None),
        )


def _arguments(text: str) -> dict[str, Any] | None:
    try:
        value = json.loads(text)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None
