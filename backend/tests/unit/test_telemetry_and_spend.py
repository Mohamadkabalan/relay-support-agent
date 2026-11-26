"""Redaction for traces and logs, and the spend cap's worst-case estimate."""

from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import Status, StatusCode

from relay.agent.manifest import ToolDef
from relay.agent.providers.base import ModelRequest
from relay.agent.spend import worst_case_micros
from relay.telemetry.logging import redact
from relay.telemetry.redaction import MASK, redact_mapping, redact_text
from relay.telemetry.tracing import RedactingExporter


def test_free_text_is_masked_by_pattern() -> None:
    text = (
        "Write to jo.smith+x@mail.example.org, call +1 (614) 555-0100 or 614.555.0199, card "
        "4111-1111-1111-1111, ship to 2210 East Cesar Chavez Street, Unit 804, Austin, TX 78702."
    )
    assert redact_text(text) == (
        "Write to [email], call [phone] or [phone], card [card], ship to [address], [unit], "
        "Austin, [postal]."
    )


def test_ids_and_amounts_survive() -> None:
    text = "PV-800101: $349.99, trace 4bf92f3577b34da6a3ce929d0e0e4736, RMA-100001, 3 items"
    assert redact_text(text) == text


def test_known_fields_are_masked_by_key() -> None:
    masked = redact_mapping(
        {"customer.email": "a@b.co", "line1": "1 Main St", "gen_ai.tool.name": "get_order", "n": 3}
    )
    assert masked == {
        "customer.email": MASK,
        "line1": MASK,
        "gen_ai.tool.name": "get_order",
        "n": 3,
    }


def test_the_exporter_redacts_attributes_and_status() -> None:
    inner = InMemorySpanExporter()
    span = ReadableSpan(
        name="relay.turn",
        attributes={"input.value": "I'm at 77 Prospect Place", "relay.turn.seq": 2},
        status=Status(StatusCode.ERROR, "failed for dana@example.com"),
    )
    RedactingExporter(inner).export([span])
    (sent,) = inner.get_finished_spans()
    assert dict(sent.attributes or {}) == {"input.value": "I'm at [address]", "relay.turn.seq": 2}
    assert sent.status.description == "failed for [email]"


def test_log_events_are_redacted() -> None:
    event = redact(
        None, "info", {"event": "Refund for dana@example.com", "phone": "x", "level": "info"}
    )
    assert event == {"event": "Refund for [email]", "phone": MASK, "level": "info"}


def test_the_worst_case_prices_every_token_at_full_price() -> None:
    tool = ToolDef("get_order", "d" * 297, {"type": "object"}, read_only=True, status_label="…")
    request = ModelRequest(
        system="s" * 3000, tools=[tool], transcript=["t" * 2700], max_tokens=16_000
    )
    # (3000 + 2700 + 297 + 18 characters) / 3 + 1 = 2006 input tokens at $4 per million, plus
    # 16,000 output tokens at $20 per million: 8,024 + 320,000 micros, plus one for rounding.
    assert worst_case_micros("claude-opus-5-5", request) == 328_025
    assert worst_case_micros("unknown-model", request) >= worst_case_micros(
        "claude-opus-5-5", request
    )
