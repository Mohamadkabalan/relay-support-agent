"""Running the model's tool calls (PLAN.md §5.8): scheduling, duplicates, limits and retries."""

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from relay.agent.manifest import Manifest, ToolDef
from relay.agent.providers.base import ToolCall
from relay.agent.tools import ToolAnswer, TurnCalls, execute
from relay.domain.enums import ToolCallOutcome


def tool(name: str, *, read_only: bool) -> ToolDef:
    schema = {"type": "object", "properties": {}, "additionalProperties": False}
    return ToolDef(name, name, schema, read_only, f"{name}…")


MANIFEST = Manifest(
    (
        tool("read_a", read_only=True),
        tool("read_b", read_only=True),
        tool("write_x", read_only=False),
        tool("write_y", read_only=False),
    )
)


@dataclass
class FakeSession:
    """Answers after a per-tool delay and logs when each call started and finished."""

    delays: dict[str, float] = field(default_factory=dict)
    failures: dict[str, list[BaseException | ToolAnswer]] = field(default_factory=dict)
    log: list[tuple[str, str, float]] = field(default_factory=list)

    async def list_tools(self) -> Manifest:
        return MANIFEST

    async def call(self, name: str, arguments: dict[str, Any]) -> ToolAnswer:
        self.log.append(("start", name, time.monotonic()))
        await asyncio.sleep(self.delays.get(name, 0))
        planned = self.failures.get(name)
        if planned:
            outcome = planned.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome
        self.log.append(("end", name, time.monotonic()))
        return ToolAnswer(text=f'{{"tool":"{name}","args":{arguments}}}', is_error=False)

    def order(self, event: str) -> list[str]:
        return [name for kind, name, _ in self.log if kind == event]


def call(name: str, n: int = 0, **arguments: Any) -> ToolCall:
    return ToolCall(f"toolu_{name}_{n}", name, arguments)


async def test_reads_run_concurrently_and_results_keep_the_models_order() -> None:
    session = FakeSession(delays={"read_a": 0.2, "read_b": 0.05})
    calls = [call("read_a"), call("read_b")]

    started = time.monotonic()
    done = await execute(calls, session, MANIFEST, TurnCalls(16))
    elapsed = time.monotonic() - started

    assert elapsed < 0.35  # not 0.25 in sequence plus overhead
    assert session.order("end") == ["read_b", "read_a"]  # b finished first...
    assert [d.call.name for d in done] == ["read_a", "read_b"]  # ...but results keep the order
    assert [d.result.call_id for d in done] == ["toolu_read_a_0", "toolu_read_b_0"]
    assert all(d.outcome is ToolCallOutcome.OK and d.attempts == 1 for d in done)


async def test_writes_run_one_at_a_time_in_order_after_the_reads() -> None:
    session = FakeSession(delays={"write_x": 0.05, "read_a": 0.05})
    calls = [call("write_x"), call("read_a"), call("write_y")]

    done = await execute(calls, session, MANIFEST, TurnCalls(16))

    assert session.order("start") == ["read_a", "write_x", "write_y"]
    assert session.order("end") == ["read_a", "write_x", "write_y"]
    assert [d.call.name for d in done] == ["write_x", "read_a", "write_y"]


async def test_the_same_call_twice_in_a_turn_is_answered_from_the_first() -> None:
    session = FakeSession()
    turn = TurnCalls(16)

    first = await execute(
        [call("read_a", 1, order="PV-1"), call("read_a", 2, order="PV-1")], session, MANIFEST, turn
    )
    later = await execute(
        [call("read_a", 3, order="PV-1"), call("read_a", 4, order="PV-2")], session, MANIFEST, turn
    )

    assert session.order("start") == ["read_a", "read_a"]  # PV-1 once, PV-2 once
    assert [d.outcome for d in first] == [ToolCallOutcome.OK, ToolCallOutcome.DUPLICATE]
    assert [d.outcome for d in later] == [ToolCallOutcome.DUPLICATE, ToolCallOutcome.OK]
    duplicate = later[0].result
    assert duplicate.call_id == "toolu_read_a_3"
    assert duplicate.content.startswith("[DUPLICATE]")
    assert first[0].result.content in duplicate.content


async def test_calls_the_model_was_told_to_retry_are_not_short_circuited() -> None:
    internal = ToolAnswer("[INTERNAL] Something went wrong. Try once more.", is_error=True)
    not_found = ToolAnswer("[ORDER_NOT_FOUND] No such order.", is_error=True)
    session = FakeSession(failures={"read_a": [internal], "read_b": [not_found]})
    turn = TurnCalls(16)

    first = await execute([call("read_a"), call("read_b")], session, MANIFEST, turn)
    again = await execute([call("read_a", 1), call("read_b", 1)], session, MANIFEST, turn)

    assert [(d.outcome, d.error_code) for d in first] == [
        (ToolCallOutcome.ERROR, "INTERNAL"),
        (ToolCallOutcome.ERROR, "ORDER_NOT_FOUND"),
    ]
    assert again[0].outcome is ToolCallOutcome.OK  # retried for real
    assert again[1].outcome is ToolCallOutcome.DUPLICATE  # same answer as before
    assert again[1].result.is_error


async def test_a_timeout_is_retried_once_then_reported() -> None:
    session = FakeSession(delays={"read_b": 0.2}, failures={"read_a": [TimeoutError()]})

    done = await execute(
        [call("read_a"), call("read_b")], session, MANIFEST, TurnCalls(16, timeout_s=0.05)
    )

    recovered, unavailable = done
    assert (recovered.outcome, recovered.attempts) == (ToolCallOutcome.OK, 2)
    assert (unavailable.outcome, unavailable.error_code) == (
        ToolCallOutcome.UNAVAILABLE,
        "UNAVAILABLE",
    )
    assert unavailable.attempts == 2
    assert unavailable.result.is_error
    assert unavailable.result.content.startswith("[UNAVAILABLE]")


async def test_calls_that_cannot_run_are_refused_without_reaching_the_server() -> None:
    session = FakeSession()
    calls = [
        ToolCall("toolu_1", "drop_tables", {}),
        ToolCall("toolu_2", "read_a", None),
        call("read_a"),
        call("read_b"),
    ]

    done = await execute(calls, session, MANIFEST, TurnCalls(max_tool_calls=3))

    assert [(d.outcome, d.error_code) for d in done] == [
        (ToolCallOutcome.REJECTED, "INVALID_ARGUMENT"),
        (ToolCallOutcome.REJECTED, "INVALID_ARGUMENT"),
        (ToolCallOutcome.OK, None),
        (ToolCallOutcome.REJECTED, "TOO_MANY_CALLS"),
    ]
    assert session.order("start") == ["read_a"]
    assert all(d.result.is_error for d in done if d.outcome is ToolCallOutcome.REJECTED)
