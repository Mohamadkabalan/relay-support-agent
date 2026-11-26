"""The byte-prefix replay invariant, on the real Claude provider and SDK (PLAN.md §5.4).

Each request of a conversation must begin with exactly the messages of the request before it,
with the same system prompt and tool definitions. That keeps the prompt cache warm and is what
Claude Opus 5.5's preserved-thinking check requires. The bodies compared here are the HTTP request
bodies the SDK actually sent.
"""

import itertools
import json

from anthropic import AsyncAnthropic
from tests.integration.agent.conftest import MakeHarness
from tests.support.agent import assert_valid_transcript
from tests.support.factories import Factory
from tests.support.fake_apis import Recorder, claude_message, claude_stream

from relay.agent.providers.anthropic import AnthropicProvider
from relay.domain.enums import TurnStatus


async def test_each_request_extends_the_last_one_byte_for_byte(
    make_harness: MakeHarness, factory: Factory
) -> None:
    customer = await factory.customer("Dana Whitfield")
    thinking = {"type": "thinking", "thinking": "Look up the account.", "signature": "sig-abc"}
    recorder = Recorder(
        [
            claude_stream(
                claude_message(
                    [
                        thinking,
                        {"type": "text", "text": "Let me look."},
                        {"type": "tool_use", "id": "toolu_01", "name": "get_customer", "input": {}},
                    ],
                    stop_reason="tool_use",
                )
            ),
            claude_stream(claude_message([{"type": "text", "text": "You're Dana Whitfield."}])),
            claude_stream(claude_message([{"type": "text", "text": "You're welcome!"}])),
        ]
    )
    client = AsyncAnthropic(api_key="test-key", http_client=recorder.client(), max_retries=0)
    harness = make_harness(provider=AnthropicProvider(client, model="claude-opus-5-5"))
    conversation_id = await harness.start(customer.id)

    first = await harness.say(conversation_id, "Who am I?")
    second = await harness.say(conversation_id, "Thanks!")

    assert (first.status, second.status) == (TurnStatus.COMPLETED, TurnStatus.COMPLETED)
    bodies = recorder.bodies()
    assert len(bodies) == 3
    for body in bodies:
        assert body["thinking"]["block_binding"] == {"prefix_mismatch_behavior": "error"}
        assert_valid_transcript(body["messages"])
    for before, after in itertools.pairwise(bodies):
        prefix = before["messages"]
        assert json.dumps(after["messages"][: len(prefix)]) == json.dumps(prefix)
        assert json.dumps(after["system"]) == json.dumps(before["system"])
        assert json.dumps(after["tools"]) == json.dumps(before["tools"])
    # The thinking block goes back exactly as the API sent it, signature included.
    assert bodies[1]["messages"][2]["content"][0] == {
        "signature": "sig-abc",
        "thinking": "Look up the account.",
        "type": "thinking",
    }
    tool_result = bodies[1]["messages"][3]["content"][0]
    assert tool_result["tool_use_id"] == "toolu_01"
    assert json.loads(tool_result["content"])["full_name"] == "Dana Whitfield"
