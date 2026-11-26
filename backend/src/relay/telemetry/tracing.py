"""OpenTelemetry tracing to Phoenix (§10), with redaction in the export path.

Each customer turn is one trace, rooted at `relay.turn`; Phoenix groups a conversation's traces
into one session through `session.id`. The Anthropic SDK's spans (metadata only) and the MCP
SDK's `tools/call` spans nest under ours automatically, and the MCP SDK carries the trace
context to the MCP server.

`RedactingExporter` wraps the real exporter, so every span is redacted before it is serialized:
nothing unredacted leaves the process. With `RELAY_TRACE_CONTENT=full` (local debugging only)
the wrapper is left out.
"""

import logging
from collections.abc import Sequence

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import Event, ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter, SpanExportResult
from opentelemetry.trace import Link, Status, format_trace_id

from relay import __version__
from relay.config import Settings
from relay.telemetry.redaction import redact_mapping, redact_text

logger = logging.getLogger(__name__)

# OpenInference span kinds, which Phoenix uses to draw each span.
SPAN_KIND = "openinference.span.kind"
SESSION_ID = "session.id"


class RedactingExporter(SpanExporter):
    """Masks PII in span attributes, event attributes, link attributes and status messages."""

    def __init__(self, inner: SpanExporter) -> None:
        self.inner = inner

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        return self.inner.export([redact_span(span) for span in spans])

    def shutdown(self) -> None:
        self.inner.shutdown()

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        return self.inner.force_flush(timeout_millis)


def redact_span(span: ReadableSpan) -> ReadableSpan:
    status = span.status
    if status.description:
        status = Status(status.status_code, redact_text(status.description))
    return ReadableSpan(
        name=span.name,
        context=span.context,
        parent=span.parent,
        resource=span.resource,
        attributes=redact_mapping(span.attributes or {}),
        events=[
            Event(event.name, redact_mapping(event.attributes or {}), event.timestamp)
            for event in span.events
        ],
        links=[Link(link.context, redact_mapping(link.attributes or {})) for link in span.links],
        kind=span.kind,
        status=status,
        start_time=span.start_time,
        end_time=span.end_time,
        instrumentation_scope=span.instrumentation_scope,
    )


def setup_tracing(settings: Settings, service: str) -> TracerProvider | None:
    """Install the global tracer provider for this process, if an OTLP endpoint is set."""
    if settings.otel_endpoint is None:
        return None
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    endpoint = settings.otel_endpoint.rstrip("/") + "/v1/traces"
    exporter: SpanExporter = OTLPSpanExporter(endpoint=endpoint)
    if settings.trace_content != "full":
        exporter = RedactingExporter(exporter)
    provider = TracerProvider(
        resource=Resource.create(
            {
                "service.name": service,
                "openinference.project.name": "relay",  # the Phoenix project the spans land in
                "service.version": __version__,
                "deployment.environment.name": settings.env,
            }
        )
    )
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    logger.info("Exporting traces to %s (content: %s)", endpoint, settings.trace_content)
    return provider


def current_trace_id() -> str | None:
    """The current trace ID as 32 hex digits, for audit rows and turn records."""
    context = trace.get_current_span().get_span_context()
    return format_trace_id(context.trace_id) if context.is_valid else None
