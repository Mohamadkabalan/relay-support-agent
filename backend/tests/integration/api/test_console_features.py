"""Phase 6's backend: the key-free demo agent, the dashboard metrics, trace links and drafts."""

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx2
from anthropic import AsyncAnthropic
from tests.support.api import open_stream, running_api, turn_completed
from tests.support.fake_apis import Recorder, claude_message

from relay.agent.providers.demo import HELP_TEXT, DemoProvider
from relay.clock import FrozenClock
from relay.evals.drafts import EXAMPLE, check

ROOT = Path(__file__).resolve().parents[4]


async def run_the_refund_story(client: httpx2.AsyncClient, api: Any) -> str:
    """Dana, the demo agent and the demo operator, as the browser test does it."""
    assert (await client.post("/api/auth/demo", json={"persona": "dana"})).status_code == 200
    conversation = (await client.post("/api/conversations")).json()["id"]
    path = f"/api/conversations/{conversation}"
    async with open_stream(client, f"{path}/events") as stream:
        await client.post(f"{path}/messages", json={"text": "My headphones arrived cracked."})
        await stream.until(turn_completed)
        await client.post(f"{path}/messages", json={"text": "Yes, go ahead."})
        second = await stream.until(turn_completed)
        number = next(e.data["approval_number"] for e in second if e.event == "approval.pending")
        assert (await client.post("/api/auth/demo-operator")).status_code == 200
        decided = await client.post(
            f"/api/console/approvals/{number}/decision", json={"decision": "approve"}
        )
        assert decided.status_code == 200
        await stream.until(turn_completed)
    return str(conversation)


async def test_the_demo_agent_tells_the_refund_story_without_a_model(
    api_database: str, clock: FrozenClock
) -> None:
    async with (
        running_api(api_database, clock=clock, provider=DemoProvider()) as api,
        api.client() as client,
    ):
        conversation = await run_the_refund_story(client, api)
        texts = [
            m["text"]
            for m in (await client.get(f"/api/conversations/{conversation}")).json()["messages"]
        ]
        assert texts[1].startswith("I can do this: Refund $349.99 to the Visa ending 4242")
        assert "quick review" in texts[1]
        assert texts[3].startswith("Submitted for review as APR-")
        assert texts[4] == "Good news: our team approved your refund, and it's on its way."

        other = (await client.post("/api/conversations")).json()["id"]
        await client.post(f"/api/conversations/{other}/messages", json={"text": "What's new?"})
        await api.idle()
        chat = (await client.get(f"/api/conversations/{other}")).json()
        assert chat["messages"][-1]["text"] == HELP_TEXT

        metrics = (await client.get("/api/console/metrics")).json()
        assert metrics["conversations"] == 2
        assert metrics["approval_turnaround_s"]["count"] == 1
        tools = {row["tool"]: row for row in metrics["tools"]}
        assert tools["issue_refund"]["calls"] == 2
        assert tools["list_orders"]["errors"] == 0
        assert metrics["escalation_rate"] == {"numerator": 0, "denominator": 2, "value": 0.0}
        assert metrics["cost_per_conversation_micros"]["mean"] == 0  # the demo agent is free
        assert set(metrics["definitions"]) >= {"resolution_rate", "cache_share"}

        replay = (await client.get(f"/api/console/conversations/{conversation}")).json()
        assert replay["session_url"] == f"http://localhost:6006/redirects/sessions/{conversation}"
        for turn in replay["turns"]:  # a link exactly when a tracer recorded the turn
            expected = (
                turn["trace_id"] and f"http://localhost:6006/redirects/traces/{turn['trace_id']}"
            )
            assert turn["trace_url"] == (expected or None)


SCENARIO = """\
id: draft-cracked-headphones-over-limit
title: Cracked headphones over the auto-approve limit go to an operator
category: hitl
fixtures:
  customers:
    - {ref: me, name: Robin Vale}
  orders:
    - ref: o1
      customer: me
      delivered_days_ago: 8
      lines:
        - {sku: AUD-ANC700, quantity: 1}
session_customer: me
customer:
  goal: "Get a refund for the cracked headphones from order {o1.number}."
  first_message: "My headphones from {o1.number} arrived with a cracked headband."
operator: {decision: approve}
expect:
  db:
    refunds:
      - {order: o1, reason: damaged_or_defective, amount_cents: 34999}
oracle:
  - tool: issue_refund
    args:
      order_number: "{o1.number}"
      reason: damaged_or_defective
      lines: [{line: 1, quantity: 1}]
"""


def drafter_answering(yaml_text: str) -> tuple[AsyncAnthropic, Recorder]:
    body = json.dumps({"scenario_yaml": yaml_text, "rationale": "Checks the approval path."})
    recorder = Recorder(
        [httpx2.Response(200, json=claude_message([{"type": "text", "text": body}]))]
    )
    client = AsyncAnthropic(api_key="test-key", http_client=recorder.client(), max_retries=0)
    return client, recorder


async def wait_for_draft(client: httpx2.AsyncClient, draft_id: str) -> dict[str, Any]:
    async with asyncio.timeout(10):
        while True:
            draft = (await client.get(f"/api/console/scenario-drafts/{draft_id}")).json()
            if draft["status"] != "generating":
                return dict(draft)
            await asyncio.sleep(0.05)


async def test_a_draft_is_valid_only_without_real_customer_details(
    api_database: str, clock: FrozenClock
) -> None:
    leaky = SCENARIO.replace("Robin Vale", "Dana Whitfield")
    for yaml_text, expected in ((SCENARIO, "valid"), (leaky, "invalid")):
        drafter, recorder = drafter_answering(yaml_text)
        async with (
            running_api(api_database, clock=clock, provider=DemoProvider(), drafter=drafter) as api,
            api.client() as client,
        ):
            conversation = await run_the_refund_story(client, api)
            created = await client.post(
                f"/api/console/conversations/{conversation}/scenario-drafts",
                json={"note": "Right outcome: refund after approval."},
            )
            assert created.status_code == 202
            assert created.json()["status"] == "generating"
            draft = await wait_for_draft(client, created.json()["id"])
            assert draft["status"] == expected, draft["problems"]
            if expected == "invalid":
                assert any("real customer detail" in p for p in draft["problems"])

        sent = recorder.bodies()[0]
        assert sent["model"] == "claude-opus-5-5"
        assert sent["output_config"]["format"]["type"] == "json_schema"
        prompt = sent["messages"][0]["content"]
        assert "Right outcome: refund after approval." in prompt
        assert "TOOL RESULT issue_refund" in prompt


async def test_drafting_without_a_model_key_is_503(api_database: str, clock: FrozenClock) -> None:
    async with running_api(api_database, clock=clock) as api, api.client() as client:
        await client.post("/api/auth/demo", json={"persona": "leo"})
        conversation = (await client.post("/api/conversations")).json()["id"]
        await client.post("/api/auth/demo-operator")
        response = await client.post(
            f"/api/console/conversations/{conversation}/scenario-drafts", json={"note": "x"}
        )
    assert response.status_code == 503


def test_draft_checks() -> None:
    assert check(SCENARIO, ["Dana Whitfield", "dana.whitfield@example.com"]) == []
    problems = check(SCENARIO.replace("Robin Vale", "Robin"), ["Robin"])
    assert problems == ["contains a real customer detail (Ro…)"]
    emailed = SCENARIO.replace(
        "{ref: me, name: Robin Vale}", "{ref: me, name: R, email: r@mail.com}"
    )
    assert "contains an email address" in check(emailed, [])
    assert check(SCENARIO.replace("AUD-ANC700", "NOPE-1"), []) != []
    assert check("id: [", [])
    assert check("just text", []) == ["the YAML is not a mapping"]


def test_the_packaged_example_is_the_suite_scenario() -> None:
    suite = ROOT / "evals/scenarios/happy-refund-damaged-under-limit.yaml"
    assert EXAMPLE.read_text() == suite.read_text()
    assert check(EXAMPLE.read_text(), []) == []
