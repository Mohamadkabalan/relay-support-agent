"""Fake model APIs for the provider contract tests: canned responses behind a mock transport.

`claude_stream` turns a final Messages API response into the server-sent events the API streams
for it, so the real SDK parses it the way it parses live traffic.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx2

Answer = httpx2.Response | Callable[[dict[str, Any]], httpx2.Response]


@dataclass
class Recorder:
    """A mock transport that answers with queued responses and keeps every request.

    A queued answer can be a function of the request body, for replies that depend on what the
    loop sent (a confirmation token from an earlier tool result, say).
    """

    responses: list[Answer]
    requests: list[httpx2.Request] = field(default_factory=list)

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        answer = self.responses.pop(0)
        if callable(answer):
            return answer(json.loads(request.content))
        return answer

    def client(self) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(transport=httpx2.MockTransport(self.handle))

    def bodies(self) -> list[dict[str, Any]]:
        return [json.loads(request.content) for request in self.requests]


def claude_message(
    content: list[dict[str, Any]],
    *,
    stop_reason: str = "end_turn",
    model: str = "claude-opus-5-5",
    usage: dict[str, Any] | None = None,
    stop_details: dict[str, Any] | None = None,
    input_transformations: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "stop_details": stop_details,
        "usage": usage
        or {
            "input_tokens": 50,
            "output_tokens": 20,
            "cache_read_input_tokens": 4000,
            "cache_creation_input_tokens": 100,
        },
        "input_transformations": input_transformations or [],
    }


def claude_stream(message: dict[str, Any], request_id: str = "req_test") -> httpx2.Response:
    """The SSE stream the Messages API sends for `message`."""
    events: list[tuple[str, dict[str, Any]]] = []
    usage = message["usage"]
    start = {
        **message,
        "content": [],
        "stop_reason": None,
        "stop_details": None,
        "usage": {**usage, "output_tokens": 1},
    }
    events.append(("message_start", {"type": "message_start", "message": start}))
    for index, block in enumerate(message["content"]):
        kind = block["type"]
        if kind == "text":
            opening: dict[str, Any] = {"type": "text", "text": ""}
            deltas = [{"type": "text_delta", "text": block["text"]}]
        elif kind == "tool_use":
            opening = {**block, "input": {}}
            deltas = [{"type": "input_json_delta", "partial_json": json.dumps(block["input"])}]
        elif kind == "thinking":
            opening = {"type": "thinking", "thinking": "", "signature": ""}
            deltas = [
                {"type": "thinking_delta", "thinking": block["thinking"]},
                {"type": "signature_delta", "signature": block["signature"]},
            ]
        else:  # fallback and other blocks arrive whole
            opening, deltas = block, []
        events.append(
            (
                "content_block_start",
                {"type": "content_block_start", "index": index, "content_block": opening},
            )
        )
        for delta in deltas:
            events.append(
                (
                    "content_block_delta",
                    {"type": "content_block_delta", "index": index, "delta": delta},
                )
            )
        events.append(("content_block_stop", {"type": "content_block_stop", "index": index}))
    events.append(
        (
            "message_delta",
            {
                "type": "message_delta",
                "delta": {
                    "stop_reason": message["stop_reason"],
                    "stop_sequence": None,
                    "stop_details": message["stop_details"],
                },
                "usage": usage,
            },
        )
    )
    events.append(("message_stop", {"type": "message_stop"}))
    body = "".join(f"event: {name}\ndata: {json.dumps(data)}\n\n" for name, data in events)
    return httpx2.Response(
        200,
        headers={"content-type": "text/event-stream", "request-id": request_id},
        content=body.encode(),
    )


def openai_response(
    output: list[dict[str, Any]],
    *,
    status: str = "completed",
    incomplete_reason: str | None = None,
    model: str = "gpt-5.5-2026-04-23",
    usage: dict[str, Any] | None = None,
) -> httpx2.Response:
    body = {
        "id": "resp_test",
        "object": "response",
        "created_at": 1_790_000_000,
        "model": model,
        "status": status,
        "incomplete_details": {"reason": incomplete_reason} if incomplete_reason else None,
        "output": output,
        "parallel_tool_calls": True,
        "tool_choice": "auto",
        "tools": [],
        "usage": usage
        or {
            "input_tokens": 1200,
            "input_tokens_details": {"cached_tokens": 1000},
            "output_tokens": 80,
            "output_tokens_details": {"reasoning_tokens": 40},
            "total_tokens": 1280,
        },
    }
    return httpx2.Response(200, headers={"x-request-id": "req_openai"}, json=body)
