# ADR-0010: Phoenix for tracing, with redaction in the export path

- **Status:** Accepted
- **Date:** 2026-11-26

## Context

An operator (and a reviewer) needs to see what one conversation did: every turn, model call, tool call, guard check and policy decision, with tokens, cost and latency. Conversations contain names, emails, phone numbers and addresses. The trace store should be self-hosted, one container, and speak OpenTelemetry so we aren't tied to it.

## Decision

- **Arize Phoenix** (`arizephoenix/phoenix:version-22.0.1-nonroot`), keeping its data in its own database on our Postgres. Self-hosted Langfuse v3 would also need ClickHouse and S3-compatible storage.
- **OpenTelemetry SDK 1.45.2 with the OTLP/HTTP exporter**, to `http://phoenix:6006/v1/traces`.
- **One trace per turn**, rooted at `relay.turn`; every span of ours carries `session.id`, the conversation ID, which is how Phoenix groups a conversation's traces into a session. A whole conversation can't be one trace: approvals leave gaps of hours. Children: `relay.model_call` (with the Anthropic SDK's own metadata-only span inside), `relay.tool_call` (with the MCP server's `tools/call` span, joined through the MCP SDK's trace propagation), `relay.guard.input`, `relay.guard.output` and `relay.policy.evaluate`. Spans carry OpenInference kinds (AGENT, LLM, TOOL, GUARDRAIL) so Phoenix draws them properly, in the Phoenix project `relay`. HTTP requests get FastAPI's native server span (FastAPI 0.165 traces requests itself), named by route; its per-dependency spans, metrics and log export are off, and the health probes are excluded.
- **Redaction wraps the exporter** (`relay/telemetry/tracing.py`): known PII keys are masked, and free text is masked by pattern (emails, phones, card-like numbers, street addresses, unit numbers, state plus ZIP). Nothing unredacted leaves the process. `RELAY_TRACE_CONTENT` puts redacted message previews on turn spans (`redacted`, the default), none at all (`none`), or unredacted ones for local debugging (`full`, refused in production).
- **Logs** are JSON through structlog, with trace and request IDs and the same redaction.
- **Trace IDs are stored** on each turn row and on audit rows, so the console can link to the trace.

## Consequences

- A test runs a PII-heavy conversation through the redacting exporter and finds no email, phone number, street address, ZIP code or card number in any exported span.
- Pattern redaction is broad: it may mask harmless text that looks like an address. That costs nothing in a trace.
- Names in free text are not detected; they appear in redacted previews. The `none` mode removes previews entirely.
- Phoenix 22 turns on authentication for any non-loopback bind unless `PHOENIX_ENABLE_AUTH` is set, and then answers our exporter with 401. The local stack, published on 127.0.0.1 only, sets it to `false`. Production (Phase 8) enables it and passes an API key through `OTEL_EXPORTER_OTLP_HEADERS`, which the exporter reads itself.
- Switching to another OpenTelemetry backend changes one endpoint, not the instrumentation.
- Database spans are not instrumented (that needs a beta instrumentation package); query time shows up inside the tool-call spans.
