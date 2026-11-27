"""The live path of a run, against a fake Claude API: the real provider and the model-played
customer inside a trial, without a key or any spending."""

import json
from pathlib import Path
from typing import Any

import httpx2
from anthropic import AsyncAnthropic
from tests.support.factories import server_url
from tests.support.fake_apis import Recorder, claude_message, claude_stream

from relay.config import Settings
from relay.domain.policy_config import default_policy
from relay.evals import harness
from relay.evals.databases import Template
from relay.evals.judge import CRITERIA, RUBRIC_VERSION
from relay.evals.scenario import load_scenarios
from relay.evals.trial import run_trial
from relay.kb.embeddings import HashingEmbedder

ROOT = Path(__file__).resolve().parents[4] / "evals"


def judge_says(
    *, failing: tuple[str, ...] = (), claims: list[dict[str, str]] | None = None
) -> httpx2.Response:
    verdict = {
        "criteria": {
            key: {"reason": "Cites the conversation.", "pass": key not in failing}
            for key in CRITERIA
        },
        "claims": claims or [],
    }
    body = claude_message(
        [{"type": "text", "text": json.dumps(verdict)}], model="claude-sonnet-5-5"
    )
    body["usage"] = {
        "input_tokens": 1000,
        "output_tokens": 200,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    }
    return httpx2.Response(200, json=body)


def simulator_says(text: str) -> httpx2.Response:
    body = claude_message([{"type": "text", "text": text}])
    body["usage"] = {
        "input_tokens": 300,
        "output_tokens": 40,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    }
    return httpx2.Response(200, json=body)


async def test_a_trial_with_the_live_provider_and_simulator(
    live_settings: Settings, light_template: Template, tmp_path: Path
) -> None:
    scenario = next(
        s for s in load_scenarios([ROOT / "scenarios"]) if s.id == "happy-track-package"
    )

    def agent_answers(body: dict[str, Any]) -> httpx2.Response:
        result = json.loads(body["messages"][-1]["content"][0]["content"])
        tracking = result["tracking_number"]
        return claude_stream(
            claude_message(
                [{"type": "text", "text": f"It's in transit with UPS, tracking {tracking}."}]
            )
        )

    tool_call = {
        "type": "tool_use",
        "id": "toolu_1",
        "name": "track_shipment",
        "input": {"order_number": "PV-900001"},
    }
    recorder = Recorder(
        [
            claude_stream(claude_message([tool_call], stop_reason="tool_use")),
            agent_answers,
            simulator_says("Great, thanks! <done/>"),
            judge_says(failing=("concise",)),
        ]
    )
    client = AsyncAnthropic(api_key="test-key", http_client=recorder.client(), max_retries=0)
    setup = harness.live_setup(
        live_settings,
        light_template,
        server_url(),
        default_policy(),
        HashingEmbedder(),
        transcripts=tmp_path,
        client=client,
    )

    row = await run_trial(setup, scenario, 1)

    assert row["success"], row["checks"]
    assert row["stop_reason"] == "done"
    assert row["simulator_calls"] == 1
    assert row["simulator_cost_micros"] == 300 * 4 + 40 * 20
    assert row["agent_cost_complete"]
    assert row["served_models"] == {"claude-opus-5-5": 2}
    bodies = recorder.bodies()
    assert [b.get("stream", False) for b in bodies] == [True, True, False, False]
    assert bodies[0]["thinking"]["block_binding"] == {"prefix_mismatch_behavior": "error"}
    simulator = bodies[2]
    assert "Nina Alvarez" in simulator["system"][0]["text"]
    assert simulator["output_config"] == {"effort": "low"}
    assert simulator["messages"][1]["role"] == "assistant"  # the scripted opening, in its voice
    assert "tracking" in simulator["messages"][2]["content"]  # only what the customer saw

    judge = bodies[3]
    assert judge["model"] == "claude-sonnet-5-5"
    assert judge["output_config"]["effort"] == "medium"
    assert judge["output_config"]["format"]["type"] == "json_schema"
    material = judge["messages"][0]["content"]
    assert "<customer_goal>" in material
    assert "TOOL RESULT track_shipment" in material
    assert "RLY-" not in material  # the policy summary, never the canary
    assert row["judge"]["criteria"]["concise"]["pass"] is False
    assert row["judge"]["all_pass"] is False
    assert row["judge"]["rubric"] == RUBRIC_VERSION
    assert row["judge_cost_micros"] == 1000 * 2 + 200 * 10
    assert row["success"]  # the judge is reported apart from task success
    transcript = json.loads((tmp_path / "happy-track-package.1.json").read_text())
    assert transcript["material"] == material


async def test_a_claimed_action_the_database_does_not_back_is_v4(
    live_settings: Settings, light_template: Template
) -> None:
    scenario = next(
        s for s in load_scenarios([ROOT / "scenarios"]) if s.id == "happy-track-package"
    )
    recorder = Recorder(
        [
            claude_stream(claude_message([{"type": "text", "text": "I've refunded your order."}])),
            simulator_says("<done/>"),
            judge_says(claims=[{"kind": "refund_issued", "quote": "I've refunded your order."}]),
        ]
    )
    client = AsyncAnthropic(api_key="test-key", http_client=recorder.client(), max_retries=0)
    setup = harness.live_setup(
        live_settings,
        light_template,
        server_url(),
        default_policy(),
        HashingEmbedder(),
        transcripts=None,
        client=client,
    )

    row = await run_trial(setup, scenario, 1)

    assert not row["success"]
    assert [v["id"] for v in row["violations"]] == ["V4"]
    assert row["violations"][0]["severity"] == "high"
    assert row["judge"]["unbacked_claims"] == [
        {"kind": "refund_issued", "quote": "I've refunded your order."}
    ]


async def test_a_failed_judge_call_leaves_the_trial_graded(
    live_settings: Settings, light_template: Template
) -> None:
    scenario = next(
        s for s in load_scenarios([ROOT / "scenarios"]) if s.id == "happy-track-package"
    )
    refusal = claude_message([], stop_reason="refusal", model="claude-sonnet-5-5")
    recorder = Recorder(
        [
            claude_stream(claude_message([{"type": "text", "text": "Let me check."}])),
            simulator_says("<done/>"),
            httpx2.Response(200, json=refusal),
        ]
    )
    client = AsyncAnthropic(api_key="test-key", http_client=recorder.client(), max_retries=0)
    setup = harness.live_setup(
        live_settings,
        light_template,
        server_url(),
        default_policy(),
        HashingEmbedder(),
        transcripts=None,
        client=client,
    )

    row = await run_trial(setup, scenario, 1)

    assert row["judge"] == {"error": "judge stopped with refusal"}
    assert row["violations"] == []
