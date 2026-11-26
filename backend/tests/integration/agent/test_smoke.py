"""The `make smoke` harness itself, run against a fake Claude so it needs no key.

The real run (`make smoke`) talks to the live API; this checks the parts around it: the
throwaway database and fixture, the customer driver that reacts to the database, and the checks.
"""

import json
from typing import Any

import httpx2
from anthropic import AsyncAnthropic
from sqlalchemy import text
from tests.support.factories import make_engine
from tests.support.fake_apis import Recorder, claude_message, claude_stream

from relay.agent.chat import TerminalView
from relay.agent.providers.anthropic import AnthropicProvider
from relay.agent.smoke import run_smoke
from relay.config import Settings

REFUND = {
    "order_number": "PV-900001",
    "reason": "damaged_or_defective",
    "lines": [{"line": 1, "quantity": 1}],
}


def tool_call(arguments: dict[str, Any]) -> httpx2.Response:
    block = {"type": "tool_use", "id": "toolu_x", "name": "issue_refund", "input": arguments}
    return claude_stream(claude_message([block], stop_reason="tool_use"))


def say(text: str) -> httpx2.Response:
    return claude_stream(claude_message([{"type": "text", "text": text}]))


def commit(body: dict[str, Any]) -> httpx2.Response:
    results = [
        block["content"]
        for message in body["messages"]
        if message["role"] == "user"
        for block in message["content"]
        if block["type"] == "tool_result"
    ]
    preview = json.loads(results[-1])  # turn 1's preview
    assert preview["status"] == "preview"
    return tool_call({**REFUND, "confirmation_token": preview["confirmation_token"]})


async def test_the_smoke_run_passes_when_the_refund_goes_through(live_settings: Settings) -> None:
    recorder = Recorder(
        [
            tool_call(REFUND),
            say("I can refund $59.99 to your Visa ending 4242. Shall I go ahead?"),
            commit,
            say("Done: your refund is on its way."),
        ]
    )
    client = AsyncAnthropic(api_key="test-key", http_client=recorder.client(), max_retries=0)
    provider = AnthropicProvider(client, model="claude-opus-5-5")
    out: list[str] = []

    result = await run_smoke(live_settings, TerminalView(out.append), provider)

    printed = "".join(out)
    assert result.passed, printed
    assert [label for label, ok in result.checks if ok] == [
        "a refund was issued",
        "no turn failed",
        "every turn completed",
        "the second model call read the cache",
    ]
    assert "you> Yes, please go ahead with that refund." in printed
    assert "PASS  a refund was issued" in printed
    engine = make_engine(str(live_settings.database_url))
    try:
        async with engine.connect() as connection:
            leftovers = await connection.scalar(
                text("SELECT count(*) FROM pg_database WHERE datname LIKE 'relay_smoke_%'")
            )
    finally:
        await engine.dispose()
    assert leftovers == 0  # the throwaway database was dropped
