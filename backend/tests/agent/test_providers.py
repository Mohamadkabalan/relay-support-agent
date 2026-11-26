"""Provider contract tests: the real SDKs against canned API responses (PLAN.md §5.11).

Every provider must render transcript items once, replay stored payloads unchanged, map stop
reasons, usage and tool calls, and turn API failures into `ProviderError`.
"""

import json
from typing import Any

import httpx2
import pytest
from anthropic import AsyncAnthropic
from openai import AsyncOpenAI
from tests.support.fake_apis import Recorder, claude_message, claude_stream, openai_response

from relay.agent.manifest import ToolDef
from relay.agent.providers.anthropic import (
    FALLBACK_BETA,
    THINKING_BINDING_BETA,
    AnthropicProvider,
)
from relay.agent.providers.base import (
    ModelEvent,
    ModelProvider,
    ModelRequest,
    ProviderError,
    Stop,
    TextDelta,
    ToolCall,
    ToolUseStarted,
    Usage,
)
from relay.agent.providers.openai import OpenAIProvider
from relay.agent.transcript import CustomerText, SystemNote, ToolResult, ToolResults, replay

GET_ORDER = ToolDef(
    name="get_order",
    description="Get one order.",
    input_schema={
        "type": "object",
        "properties": {"order_number": {"type": "string"}},
        "required": ["order_number"],
        "additionalProperties": False,
    },
    read_only=True,
    status_label="Checking your order…",
)
ESCALATE = ToolDef(
    name="escalate_to_human",
    description="Hand off.",
    input_schema={"type": "object", "properties": {}, "additionalProperties": False},
    read_only=False,
    status_label="Connecting you with our team…",
)


class Events:
    def __init__(self) -> None:
        self.seen: list[ModelEvent] = []

    async def __call__(self, event: ModelEvent) -> None:
        self.seen.append(event)


def request(provider: ModelProvider, *items: Any, raw: list[str] = ()) -> ModelRequest:  # type: ignore[assignment]
    transcript = [provider.render(item) for item in items] + list(raw)
    return ModelRequest(
        system="You help customers.",
        tools=[GET_ORDER, ESCALATE],
        transcript=transcript,
        max_tokens=16_000,
        user_key="opaque-key",
    )


def claude(recorder: Recorder, **options: Any) -> AnthropicProvider:
    client = AsyncAnthropic(api_key="test-key", http_client=recorder.client(), max_retries=0)
    return AnthropicProvider(client, model="claude-opus-5-5", **options)


def gpt(recorder: Recorder) -> OpenAIProvider:
    client = AsyncOpenAI(api_key="test-key", http_client=recorder.client(), max_retries=0)
    return OpenAIProvider(client, model="gpt-5.5")


TOOL_TURN = claude_message(
    [
        {"type": "thinking", "thinking": "I should look the order up.", "signature": "sig-1"},
        {"type": "text", "text": "Let me check that order."},
        {
            "type": "tool_use",
            "id": "toolu_01",
            "name": "get_order",
            "input": {"order_number": "PV-800101"},
        },
    ],
    stop_reason="tool_use",
)


# ---------------------------------------------------------------- Claude


async def test_claude_request_follows_the_model_call_settings() -> None:
    recorder = Recorder([claude_stream(TOOL_TURN)])
    provider = claude(recorder)

    await provider.complete(request(provider, CustomerText("Hi")), Events())

    sent = recorder.requests[0]
    assert sent.headers["anthropic-beta"].split(",") == [THINKING_BINDING_BETA, FALLBACK_BETA]
    body = recorder.bodies()[0]
    assert body["model"] == "claude-opus-5-5"
    assert body["system"] == [
        {"type": "text", "text": "You help customers.", "cache_control": {"type": "ephemeral"}}
    ]
    assert body["cache_control"] == {"type": "ephemeral"}
    assert [tool["name"] for tool in body["tools"]] == ["escalate_to_human", "get_order"]
    assert all(tool["strict"] is True for tool in body["tools"])
    assert body["tool_choice"] == {"type": "auto"}
    assert body["thinking"] == {
        "type": "adaptive",
        "display": "summarized",
        "block_binding": {"prefix_mismatch_behavior": "error"},
    }
    assert body["output_config"] == {"effort": "medium"}
    assert body["fallbacks"] == "default"
    assert body["metadata"] == {"user_id": "opaque-key"}
    assert body["max_tokens"] == 16_000
    assert body["stream"] is True


async def test_claude_production_settings_and_no_fallbacks() -> None:
    recorder = Recorder([claude_stream(TOOL_TURN)])
    provider = claude(recorder, prefix_mismatch="drop_block", fallbacks=False, effort=None)

    await provider.complete(request(provider, CustomerText("Hi")), Events())

    body = recorder.bodies()[0]
    assert body["thinking"]["block_binding"] == {"prefix_mismatch_behavior": "drop_block"}
    assert "fallbacks" not in body
    assert "output_config" not in body
    assert recorder.requests[0].headers["anthropic-beta"] == THINKING_BINDING_BETA


async def test_claude_tool_use_response() -> None:
    recorder = Recorder([claude_stream(TOOL_TURN, request_id="req_42")])
    provider = claude(recorder)
    events = Events()

    response = await provider.complete(request(provider, CustomerText("Hi")), events)

    assert response.stop is Stop.TOOL_USE
    assert response.text == "Let me check that order."
    assert response.tool_calls == [ToolCall("toolu_01", "get_order", {"order_number": "PV-800101"})]
    assert response.usage == Usage(50, 20, 4000, 100)
    assert response.cost_micros == 50 * 4 + 20 * 20 + 4000 * 0.2 + 100 * 5
    assert (response.served_model, response.request_id) == ("claude-opus-5-5", "req_42")
    assert response.thinking == "I should look the order up."
    assert not response.fallback_used
    assert events.seen == [TextDelta("Let me check that order."), ToolUseStarted("get_order")]
    assert response.raw is not None
    assert json.loads(response.raw) == [{"role": "assistant", "content": TOOL_TURN["content"]}]


async def test_claude_mid_output_fallback_is_stored_in_the_form_the_api_expects_back() -> None:
    content: list[dict[str, Any]] = [
        {"type": "thinking", "thinking": "Hmm.", "signature": "sig-declined"},
        {"type": "text", "text": "Partial answer "},
        {"type": "tool_use", "id": "toolu_cut", "name": "get_order", "input": {}},
        {
            "type": "fallback",
            "from": {"model": "claude-opus-5-5"},
            "to": {"model": "claude-opus-5"},
            "trigger": {"type": "refusal", "category": "cyber"},
        },
        {"type": "thinking", "thinking": "Continue.", "signature": "sig-served"},
        {"type": "text", "text": "Here is your order."},
    ]
    usage = {
        "input_tokens": 10,
        "output_tokens": 30,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "iterations": [
            {
                "type": "message",
                "input_tokens": 1000,
                "output_tokens": 5,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
            },
            {
                "type": "fallback_message",
                "model": "claude-opus-5",
                "input_tokens": 1000,
                "output_tokens": 30,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
            },
        ],
    }
    message = claude_message(content, model="claude-opus-5-5", usage=usage)
    recorder = Recorder([claude_stream(message)])
    provider = claude(recorder)

    response = await provider.complete(request(provider, CustomerText("Hi")), Events())

    assert response.fallback_used
    assert response.served_model == "claude-opus-5"
    assert response.usage == Usage(input_tokens=2000, output_tokens=35)
    assert response.cost_micros == (1000 * 4 + 5 * 20) + (1000 * 5 + 30 * 25)
    assert response.tool_calls == []  # the cut-off tool call is not run
    assert response.raw is not None
    kept = json.loads(response.raw)[0]["content"]
    assert [block["type"] for block in kept] == ["text", "fallback", "thinking", "text"]


@pytest.mark.parametrize(
    ("stop_reason", "stop"), [("refusal", Stop.REFUSAL), ("max_tokens", Stop.MAX_TOKENS)]
)
async def test_claude_refusal_and_truncation_are_not_appended(stop_reason: str, stop: Stop) -> None:
    details = {"type": "refusal", "category": "cyber", "explanation": None}
    message = claude_message(
        [{"type": "text", "text": "Partial"}],
        stop_reason=stop_reason,
        stop_details=details if stop is Stop.REFUSAL else None,
    )
    recorder = Recorder([claude_stream(message)])
    provider = claude(recorder)

    response = await provider.complete(request(provider, CustomerText("Hi")), Events())

    assert response.stop is stop
    assert response.raw is None
    assert response.refusal_category == ("cyber" if stop is Stop.REFUSAL else None)


async def test_claude_api_errors_become_provider_errors() -> None:
    error = {"type": "error", "error": {"type": "invalid_request_error", "message": "bad"}}
    recorder = Recorder([httpx2.Response(400, json=error)])
    provider = claude(recorder)

    with pytest.raises(ProviderError, match="bad"):
        await provider.complete(request(provider, CustomerText("Hi")), Events())


async def test_claude_unexpected_stop_reason_is_an_error() -> None:
    recorder = Recorder([claude_stream(claude_message([], stop_reason="pause_turn"))])
    provider = claude(recorder)

    with pytest.raises(ProviderError, match="pause_turn"):
        await provider.complete(request(provider, CustomerText("Hi")), Events())


# ---------------------------------------------------------------- OpenAI


FUNCTION_CALL: list[dict[str, Any]] = [
    {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "opaque=="},
    {
        "type": "function_call",
        "id": "fc_1",
        "call_id": "call_1",
        "name": "get_order",
        "arguments": '{"order_number":"PV-800101"}',
        "status": "completed",
    },
]


async def test_openai_request_and_tool_call() -> None:
    recorder = Recorder([openai_response(FUNCTION_CALL)])
    provider = gpt(recorder)
    events = Events()

    response = await provider.complete(
        request(provider, CustomerText("Hi"), SystemNote("Current date: Tuesday.")), events
    )

    body = recorder.bodies()[0]
    assert body["instructions"] == "You help customers."
    assert body["input"] == [
        {"role": "user", "content": "Hi"},
        {"role": "developer", "content": "Current date: Tuesday."},
    ]
    assert [tool["name"] for tool in body["tools"]] == ["escalate_to_human", "get_order"]
    assert all(tool["type"] == "function" and tool["strict"] for tool in body["tools"])
    assert body["store"] is False
    assert body["include"] == ["reasoning.encrypted_content"]
    assert body["reasoning"] == {"effort": "medium"}
    assert body["safety_identifier"] == "opaque-key"

    assert response.stop is Stop.TOOL_USE
    assert response.tool_calls == [ToolCall("call_1", "get_order", {"order_number": "PV-800101"})]
    assert response.usage == Usage(input_tokens=200, output_tokens=80, cache_read_tokens=1000)
    assert response.cost_micros is None
    assert events.seen == [ToolUseStarted("get_order")]
    assert response.raw is not None
    assert json.loads(response.raw) == FUNCTION_CALL


async def test_openai_text_refusal_truncation_and_bad_arguments() -> None:
    message: dict[str, Any] = {
        "type": "message",
        "id": "msg_1",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": "All set.", "annotations": []}],
    }
    refusal = {**message, "content": [{"type": "refusal", "refusal": "I can't help with that."}]}
    bad_call = {**FUNCTION_CALL[1], "arguments": "{not json"}
    recorder = Recorder(
        [
            openai_response([message]),
            openai_response([refusal]),
            openai_response([message], status="incomplete", incomplete_reason="max_output_tokens"),
            openai_response([bad_call]),
        ]
    )
    provider = gpt(recorder)
    events = Events()

    done = await provider.complete(request(provider, CustomerText("Hi")), events)
    refused = await provider.complete(request(provider, CustomerText("Hi")), events)
    truncated = await provider.complete(request(provider, CustomerText("Hi")), events)
    unreadable = await provider.complete(request(provider, CustomerText("Hi")), events)

    assert (done.stop, done.text) == (Stop.END_TURN, "All set.")
    assert events.seen[0] == TextDelta("All set.")
    assert (refused.stop, refused.raw) == (Stop.REFUSAL, None)
    assert (truncated.stop, truncated.raw) == (Stop.MAX_TOKENS, None)
    assert unreadable.tool_calls == [ToolCall("call_1", "get_order", None)]


async def test_openai_api_errors_become_provider_errors() -> None:
    error = {"error": {"message": "nope", "type": "invalid_request_error"}}
    recorder = Recorder([httpx2.Response(400, json=error)])
    provider = gpt(recorder)

    with pytest.raises(ProviderError, match="nope"):
        await provider.complete(request(provider, CustomerText("Hi")), Events())


# ---------------------------------------------------------------- the shared contract


@pytest.mark.parametrize("name", ["anthropic", "openai"])
async def test_stored_payloads_are_replayed_unchanged(name: str) -> None:
    """The next request carries the earlier transcript exactly, with the new items after it."""
    if name == "anthropic":
        first = claude_stream(TOOL_TURN)
        second = claude_stream(claude_message([{"type": "text", "text": "It shipped."}]))
        recorder = Recorder([first, second])
        provider: ModelProvider = claude(recorder)
        key = "messages"
    else:
        done: dict[str, Any] = {
            "type": "message",
            "id": "msg_2",
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": "It shipped.", "annotations": []}],
        }
        recorder = Recorder([openai_response(FUNCTION_CALL), openai_response([done])])
        provider = gpt(recorder)
        key = "input"

    transcript = [provider.render(CustomerText("Where is PV-800101?"))]
    reply = await provider.complete(request(provider, raw=transcript), Events())
    assert reply.raw is not None
    transcript.append(reply.raw)
    results = ToolResults((ToolResult(reply.tool_calls[0].id, "get_order", '{"a":1}', False),))
    transcript.append(provider.render(results))
    await provider.complete(request(provider, raw=transcript), Events())

    first_body, second_body = recorder.bodies()
    assert second_body[key] == replay(transcript)
    prefix = first_body[key]
    assert second_body[key][: len(prefix)] == prefix
    assert json.dumps(second_body[key][: len(prefix)]) == json.dumps(prefix)
    for unchanged in ("tools", "system", "instructions"):
        assert first_body.get(unchanged) == second_body.get(unchanged)
