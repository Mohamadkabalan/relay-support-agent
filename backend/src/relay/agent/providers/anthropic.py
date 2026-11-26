"""The Claude provider: the Messages API (beta namespace), streamed, with the settings in §5.6.

Request layout (§5.4): `tools` (strict, sorted by name), then a `system` block with an explicit
cache breakpoint, then the replayed `messages`; top-level `cache_control` caches the growing tail.
Thinking is adaptive with summarized display, and `thinking.block_binding` sets what the API does
when replayed history no longer matches a thinking block's signature: "error" in development, CI
and evals, so an accidental history edit fails loudly; "drop_block" in production.

Refusals: with `fallbacks: "default"` the API re-runs a declined request on Anthropic's
recommended fallback model inside the same call. A refusal that survives it ends the turn.
After a mid-output fallback, the blocks before the last `fallback` block that the docs say to
omit (thinking, tool use, anything unrecognized) are left out of the stored payload, so the
payload we replay is exactly the form the API expects back.
"""

import time
from typing import Any, Literal

from anthropic import APIError, AsyncAnthropic
from pydantic import BaseModel

from relay.agent.manifest import ToolDef
from relay.agent.pricing import cost_micros
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

THINKING_BINDING_BETA = "thinking-binding-controls-2026-08-01"
FALLBACK_BETA = "server-side-fallback-2026-07-01"

PrefixMismatch = Literal["error", "drop_block"]

STOPS = {
    "end_turn": Stop.END_TURN,
    "stop_sequence": Stop.END_TURN,
    "tool_use": Stop.TOOL_USE,
    "max_tokens": Stop.MAX_TOKENS,
    "refusal": Stop.REFUSAL,
}

# Block types that stay in front of the final `fallback` block when a turn is sent back.
KEPT_BEFORE_FALLBACK = {"text", "fallback"}


def render(item: Item) -> str:
    match item:
        case CustomerText(text) | EventLine(text):
            return dump([{"role": "user", "content": [{"type": "text", "text": text}]}])
        case AssistantText(text):
            return dump([{"role": "assistant", "content": [{"type": "text", "text": text}]}])
        case SystemNote(text):
            return dump([{"role": "system", "content": text}])
        case ToolResults(results):
            blocks = [
                {
                    "type": "tool_result",
                    "tool_use_id": result.call_id,
                    "content": result.content,
                    "is_error": result.is_error,
                }
                for result in results
            ]
            return dump([{"role": "user", "content": blocks}])


def render_tools(tools: list[ToolDef] | tuple[ToolDef, ...]) -> list[dict[str, Any]]:
    return [
        {
            "name": tool.name,
            "description": tool.description,
            "input_schema": tool.input_schema,
            "strict": True,
        }
        for tool in sorted(tools, key=lambda tool: tool.name)
    ]


def echo_form(block: BaseModel) -> dict[str, Any]:
    """A response block the way the SDK itself sends it back: only the fields the API set."""
    return block.model_dump(
        mode="json",
        by_alias=True,
        exclude_unset=True,
        exclude=getattr(block, "__api_exclude__", None),
    )


def replayable(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop what must not be sent back from before the last fallback boundary."""
    boundary = max((i for i, block in enumerate(blocks) if block["type"] == "fallback"), default=-1)
    return [
        block
        for i, block in enumerate(blocks)
        if i >= boundary or block["type"] in KEPT_BEFORE_FALLBACK
    ]


class AnthropicProvider:
    name = "anthropic"

    def __init__(
        self,
        client: AsyncAnthropic,
        *,
        model: str,
        effort: str | None = "medium",
        fallbacks: bool = True,
        prefix_mismatch: PrefixMismatch = "error",
    ) -> None:
        self.client = client
        self.model = model
        self.effort = effort
        self.fallbacks = fallbacks
        self.prefix_mismatch = prefix_mismatch

    def render(self, item: Item) -> str:
        return render(item)

    def build_params(self, request: ModelRequest) -> dict[str, Any]:
        betas = [THINKING_BINDING_BETA]
        params: dict[str, Any] = {
            "model": self.model,
            "max_tokens": request.max_tokens,
            "tools": render_tools(list(request.tools)),
            "tool_choice": {"type": "auto"},
            "system": [
                {"type": "text", "text": request.system, "cache_control": {"type": "ephemeral"}}
            ],
            "messages": replay(list(request.transcript)),
            "cache_control": {"type": "ephemeral"},
            "thinking": {
                "type": "adaptive",
                "display": "summarized",
                "block_binding": {"prefix_mismatch_behavior": self.prefix_mismatch},
            },
        }
        if self.effort:
            params["output_config"] = {"effort": self.effort}
        if request.user_key:
            params["metadata"] = {"user_id": request.user_key}
        if self.fallbacks:
            params["fallbacks"] = "default"
            betas.append(FALLBACK_BETA)
        params["betas"] = betas
        return params

    async def complete(self, request: ModelRequest, on_event: EventSink) -> ModelResponse:
        params = self.build_params(request)
        started = time.monotonic()
        ttft_ms: int | None = None
        try:
            async with self.client.beta.messages.stream(**params) as stream:
                async for event in stream:
                    if event.type == "content_block_start":
                        if ttft_ms is None:
                            ttft_ms = _ms_since(started)
                        if event.content_block.type == "tool_use":
                            await on_event(ToolUseStarted(event.content_block.name))
                    elif event.type == "text":
                        await on_event(TextDelta(event.text))
                message = await stream.get_final_message()
                request_id = stream.request_id
        except APIError as error:
            raise ProviderError(f"Claude API error: {error}") from error
        return self._response(message, started, ttft_ms, request_id)

    def _response(
        self, message: Any, started: float, ttft_ms: int | None, request_id: str | None
    ) -> ModelResponse:
        stop = STOPS.get(message.stop_reason or "")
        if stop is None:
            raise ProviderError(f"Unexpected stop reason {message.stop_reason!r}")
        blocks = replayable([echo_form(block) for block in message.content])
        text = "".join(block["text"] for block in blocks if block["type"] == "text")
        calls = [
            ToolCall(block["id"], block["name"], block["input"])
            for block in blocks
            if block["type"] == "tool_use"
        ]
        if stop is Stop.TOOL_USE and not calls:
            raise ProviderError("stop_reason tool_use without a tool_use block")
        thinking = "\n\n".join(
            block["thinking"]
            for block in blocks
            if block["type"] == "thinking" and block["thinking"]
        )
        appendable = stop in (Stop.END_TURN, Stop.TOOL_USE) and bool(blocks)
        usage, cost, fallback_used = _usage_and_cost(message, self.model)
        category = None
        details = getattr(message, "stop_details", None)
        if stop is Stop.REFUSAL and details is not None:
            category = getattr(details, "category", None)
        return ModelResponse(
            stop=stop,
            text=text,
            tool_calls=calls,
            usage=usage,
            requested_model=self.model,
            served_model=message.model,
            raw=dump([{"role": "assistant", "content": blocks}]) if appendable else None,
            cost_micros=cost,
            latency_ms=_ms_since(started),
            ttft_ms=ttft_ms,
            request_id=request_id,
            refusal_category=category,
            fallback_used=fallback_used,
            input_transformations=[
                echo_form(entry) for entry in message.input_transformations or []
            ],
            thinking=thinking or None,
        )


def _usage_and_cost(message: Any, requested_model: str) -> tuple[Usage, int | None, bool]:
    """Usage and cost across every attempt of the call.

    With a server-side fallback, top-level `usage` covers only the attempt that produced the
    message; `usage.iterations` lists each attempt with the model that ran it.
    """
    usage = message.usage
    attempts = [
        entry for entry in usage.iterations or [] if entry.type in ("message", "fallback_message")
    ]
    fallback_used = any(entry.type == "fallback_message" for entry in attempts)
    if not attempts:
        attempts = [usage]
    total = Usage()
    cost: int | None = 0
    for entry in attempts:
        part = Usage(
            input_tokens=entry.input_tokens,
            output_tokens=entry.output_tokens,
            cache_read_tokens=entry.cache_read_input_tokens or 0,
            cache_write_tokens=entry.cache_creation_input_tokens or 0,
        )
        total += part
        model = getattr(entry, "model", None) or (
            message.model if entry is usage else requested_model
        )
        part_cost = cost_micros(
            model,
            input_tokens=part.input_tokens,
            output_tokens=part.output_tokens,
            cache_read_tokens=part.cache_read_tokens,
            cache_write_tokens=part.cache_write_tokens,
        )
        cost = None if cost is None or part_cost is None else cost + part_cost
    return total, cost, fallback_used


def _ms_since(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
