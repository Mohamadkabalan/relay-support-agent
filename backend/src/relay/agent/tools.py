"""The agent's side of MCP: one connection per turn, and the rules for running tool calls (§5.8).

Connecting:

- `HttpTools` is the production path: Streamable HTTP to the `mcp` service with a JWT minted for
  this customer, conversation and turn (ADR-0003).
- `InProcessTools` runs the same server inside this process through the SDK's in-memory
  transport, bound to the turn's scope. Tests and `relay chat --in-process` use it.

Running calls (`execute`):

- Calls to read-only tools in one response run concurrently; writes run one at a time, in the
  order the model made them, after the reads. Results come back in the model's order.
- The same tool with the same arguments twice in one turn is answered from the first result and
  not run again. At most `max_tool_calls` calls run per turn; the rest are refused.
- A call that times out or fails in transport is retried once. That is safe for every Relay
  tool: reads have no effects, a preview only issues a token, a commit carries its token (the
  server replays the first outcome), and escalating twice returns the open escalation.
"""

import asyncio
import contextlib
import json
import logging
import re
import time
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, Protocol

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp_types import CallToolResult, TextContent

from relay import turn_tokens
from relay.agent.manifest import Manifest
from relay.agent.providers.base import ToolCall
from relay.agent.transcript import ToolResult, dump
from relay.domain.enums import ToolCallOutcome
from relay.domain.scope import CustomerScope
from relay.mcp_server.identity import BoundIdentity
from relay.mcp_server.runtime import ToolRuntime
from relay.mcp_server.server import build_server

logger = logging.getLogger(__name__)

ERROR_CODE = re.compile(r"^\[([A-Z_]+)\]")


@dataclass(frozen=True, slots=True)
class ToolAnswer:
    text: str
    is_error: bool


class ToolSession(Protocol):
    async def list_tools(self) -> Manifest: ...

    async def call(self, name: str, arguments: dict[str, Any]) -> ToolAnswer: ...


class ToolConnector(Protocol):
    def connect(self, scope: CustomerScope) -> AbstractAsyncContextManager[ToolSession]: ...


class McpSession:
    """A connected MCP client."""

    def __init__(self, client: Client) -> None:
        self.client = client

    async def list_tools(self) -> Manifest:
        listed = await self.client.list_tools()
        return Manifest.from_mcp(listed.tools)

    async def call(self, name: str, arguments: dict[str, Any]) -> ToolAnswer:
        result = await self.client.call_tool(name, arguments)
        return answer_from(result)


def answer_from(result: CallToolResult) -> ToolAnswer:
    text = "".join(block.text for block in result.content if isinstance(block, TextContent))
    if not result.is_error:
        # The server pretty-prints; the model gets the same JSON without the indentation.
        with contextlib.suppress(ValueError):
            text = dump(json.loads(text))
    return ToolAnswer(text=text, is_error=bool(result.is_error))


@dataclass
class HttpTools:
    url: str
    secret: str
    timeout_s: float = 30.0
    transport: httpx2.AsyncBaseTransport | None = None  # tests serve the ASGI app directly

    @asynccontextmanager
    async def connect(self, scope: CustomerScope) -> AsyncIterator[ToolSession]:
        if scope.conversation_id is None or scope.turn_seq is None:
            raise ValueError("HTTP tool calls need a conversation and a turn")
        token = turn_tokens.mint(
            self.secret,
            customer_id=scope.customer_id,
            conversation_id=scope.conversation_id,
            turn_seq=scope.turn_seq,
            now=datetime.now(UTC),
        )
        headers = {"Authorization": f"Bearer {token}"}
        async with (
            httpx2.AsyncClient(
                headers=headers, timeout=self.timeout_s, transport=self.transport
            ) as http_client,
            Client(streamable_http_client(self.url, http_client=http_client)) as client,
        ):
            yield McpSession(client)


@dataclass
class InProcessTools:
    """The MCP server in this process, acting for the turn's customer, conversation and turn."""

    runtime: ToolRuntime
    store_name: str = "Pixel & Volt"

    @asynccontextmanager
    async def connect(self, scope: CustomerScope) -> AsyncIterator[ToolSession]:
        runtime = replace(self.runtime, identity=BoundIdentity(scope))
        server = build_server(runtime, store_name=self.store_name)
        async with Client(server) as client:
            yield McpSession(client)


# ---------------------------------------------------------------- running calls


@dataclass(frozen=True, slots=True)
class ExecutedCall:
    call: ToolCall
    result: ToolResult
    outcome: ToolCallOutcome
    error_code: str | None
    latency_ms: int
    attempts: int


@dataclass
class TurnCalls:
    """What the tools have done so far in this turn: for the call limit and duplicates."""

    max_tool_calls: int
    timeout_s: float = 10.0
    count: int = 0
    seen: dict[tuple[str, str], ToolResult] = field(default_factory=dict)


def canonical(arguments: dict[str, Any]) -> str:
    return json.dumps(arguments, sort_keys=True, separators=(",", ":"))


async def execute(
    calls: list[ToolCall], session: ToolSession, manifest: Manifest, turn: TurnCalls
) -> list[ExecutedCall]:
    done: dict[int, ExecutedCall] = {}
    reads: list[int] = []
    writes: list[int] = []
    first_of: dict[int, int] = {}  # a duplicate within this response -> the call it repeats
    in_batch: dict[tuple[str, str], int] = {}

    for i, call in enumerate(calls):
        tool = manifest.get(call.name)
        if turn.count >= turn.max_tool_calls:
            done[i] = _refused(
                call,
                "TOO_MANY_CALLS",
                "This turn has used all of its tool calls. Answer the customer with what you "
                "have, or call escalate_to_human in your next turn.",
            )
            continue
        turn.count += 1
        if tool is None:
            done[i] = _refused(call, "INVALID_ARGUMENT", f"There is no tool named {call.name}.")
            continue
        if call.arguments is None:
            done[i] = _refused(
                call, "INVALID_ARGUMENT", "The arguments were not a valid JSON object."
            )
            continue
        key = (call.name, canonical(call.arguments))
        if key in turn.seen:
            done[i] = _duplicate(call, turn.seen[key])
        elif key in in_batch:
            first_of[i] = in_batch[key]
        else:
            in_batch[key] = i
            (reads if tool.read_only else writes).append(i)

    async def run(i: int) -> None:
        call = calls[i]
        done[i] = await _run_one(call, call.arguments or {}, session, turn.timeout_s)

    async with asyncio.TaskGroup() as group:
        for i in reads:
            group.create_task(run(i))
    for i in writes:
        await run(i)

    for i, first in first_of.items():
        done[i] = _duplicate(calls[i], done[first].result)
    for i in in_batch.values():
        if _settled(done[i]):
            turn.seen[(calls[i].name, canonical(calls[i].arguments or {}))] = done[i].result
    return [done[i] for i in range(len(calls))]


def _settled(executed: ExecutedCall) -> bool:
    """Whether running the call again would give the same answer.

    Not for a call that timed out or crashed: the model is told to try those once more.
    """
    if executed.outcome is ToolCallOutcome.OK:
        return True
    return executed.outcome is ToolCallOutcome.ERROR and executed.error_code != "INTERNAL"


async def _run_one(
    call: ToolCall, arguments: dict[str, Any], session: ToolSession, timeout_s: float
) -> ExecutedCall:
    started = time.monotonic()
    for attempt in (1, 2):
        try:
            async with asyncio.timeout(timeout_s):
                answer = await session.call(call.name, arguments)
        except Exception as error:
            logger.warning("Tool call %s failed on attempt %d: %r", call.name, attempt, error)
            continue
        code = _error_code(answer.text) if answer.is_error else None
        return ExecutedCall(
            call=call,
            result=ToolResult(call.id, call.name, answer.text, answer.is_error),
            outcome=ToolCallOutcome.ERROR if answer.is_error else ToolCallOutcome.OK,
            error_code=code,
            latency_ms=_ms_since(started),
            attempts=attempt,
        )
    text = (
        f"[UNAVAILABLE] {call.name} didn't answer in time. Try once more; if it fails again, "
        "apologize and call escalate_to_human."
    )
    return ExecutedCall(
        call=call,
        result=ToolResult(call.id, call.name, text, is_error=True),
        outcome=ToolCallOutcome.UNAVAILABLE,
        error_code="UNAVAILABLE",
        latency_ms=_ms_since(started),
        attempts=2,
    )


def _refused(call: ToolCall, code: str, message: str) -> ExecutedCall:
    return ExecutedCall(
        call=call,
        result=ToolResult(call.id, call.name, f"[{code}] {message}", is_error=True),
        outcome=ToolCallOutcome.REJECTED,
        error_code=code,
        latency_ms=0,
        attempts=0,
    )


def _duplicate(call: ToolCall, first: ToolResult) -> ExecutedCall:
    text = (
        "[DUPLICATE] You already made this exact call in this turn, so it was not run again. "
        f"Its result was: {first.content}"
    )
    return ExecutedCall(
        call=call,
        result=ToolResult(call.id, call.name, text, is_error=first.is_error),
        outcome=ToolCallOutcome.DUPLICATE,
        error_code=None,
        latency_ms=0,
        attempts=0,
    )


def _error_code(text: str) -> str | None:
    match = ERROR_CODE.match(text)
    return match.group(1) if match else None


def _ms_since(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
