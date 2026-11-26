"""Exported spans of a PII-heavy conversation carry no emails, phone numbers or addresses (§10).

The spans go through the same `RedactingExporter` production uses, into memory.
"""

import json
import uuid
from collections.abc import Iterator

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from tests.integration.agent.conftest import MakeHarness
from tests.support.factories import Factory

from relay.agent.providers.scripted import Call, Reply
from relay.telemetry.tracing import RedactingExporter

AGENT_SPANS = {
    "relay.turn",
    "relay.model_call",
    "relay.tool_call",
    "relay.guard.input",
    "relay.guard.output",
}
MESSAGE = (
    "Hi, it's Dana. Email me at dana.whitfield@example.com or call (614) 555-0100. I moved to "
    "418 Maple Avenue, Apt 3B, Columbus OH 43215. My card is 4242 4242 4242 4242."
)
REPLY = (
    "Thanks! I have dana.whitfield@example.com and 418 Maple Avenue, Apt 3B on file, and your "
    "phone (614) 555-0100."
)
PII = ("dana.whitfield@example.com", "555-0100", "418 Maple", "Apt 3B", "43215", "4242 4242")

_exporter = InMemorySpanExporter()


@pytest.fixture(scope="session")
def tracer_provider() -> TracerProvider:
    """The process-wide provider (OpenTelemetry allows one), exporting through the redactor."""
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(RedactingExporter(_exporter)))
    trace.set_tracer_provider(provider)
    return provider


@pytest.fixture
def exported(tracer_provider: TracerProvider) -> Iterator[InMemorySpanExporter]:
    _exporter.clear()
    yield _exporter
    _exporter.clear()


def everything(spans: tuple[ReadableSpan, ...]) -> str:
    return json.dumps(
        [
            {
                "name": span.name,
                "attributes": dict(span.attributes or {}),
                "events": [dict(event.attributes or {}) for event in span.events],
                "status": span.status.description,
            }
            for span in spans
        ],
        default=str,
    )


async def run_conversation(
    make_harness: MakeHarness, factory: Factory, *, content: bool
) -> uuid.UUID:
    customer = await factory.customer("Dana Whitfield")
    harness = make_harness([Reply(calls=(Call("get_customer", {}),)), Reply(text=REPLY)])
    harness.agent.trace_content = content
    conversation_id = await harness.start(customer.id)
    outcome = await harness.say(conversation_id, MESSAGE)
    assert outcome.replies == [REPLY]  # the customer still gets the real reply
    return conversation_id


async def test_spans_are_redacted_before_export(
    make_harness: MakeHarness, factory: Factory, exported: InMemorySpanExporter
) -> None:
    conversation_id = await run_conversation(make_harness, factory, content=True)
    spans = exported.get_finished_spans()

    assert {span.name for span in spans} >= AGENT_SPANS
    dumped = everything(spans)
    for secret in PII:
        assert secret not in dumped, secret
    turn = next(span for span in spans if span.name == "relay.turn")
    attributes = dict(turn.attributes or {})
    assert "[email]" in str(attributes["input.value"])  # the preview is there, redacted
    assert "[phone]" in str(attributes["output.value"])
    assert attributes["relay.turn.status"] == "completed"
    ours = [span for span in spans if span.name in AGENT_SPANS]
    assert {dict(span.attributes or {})["session.id"] for span in ours} == {str(conversation_id)}
    assert len({span.context.trace_id for span in ours}) == 1  # one trace per turn


async def test_no_content_on_spans_when_trace_content_is_none(
    make_harness: MakeHarness, factory: Factory, exported: InMemorySpanExporter
) -> None:
    await run_conversation(make_harness, factory, content=False)
    turn = next(span for span in exported.get_finished_spans() if span.name == "relay.turn")
    assert "input.value" not in (turn.attributes or {})
    assert "output.value" not in (turn.attributes or {})
