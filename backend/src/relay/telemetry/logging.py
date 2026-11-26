"""Structured JSON logs (§10): one JSON object per line, with trace and request IDs, redacted.

Relay's modules log through the standard library (`logging.getLogger(__name__)`), and so do
uvicorn and the SDKs. `configure_logging` routes all of it through structlog's formatter, which
adds the IDs, masks PII the same way traces do, and renders JSON.
"""

import logging
import sys
from contextvars import ContextVar
from typing import Any

import structlog
from structlog.types import EventDict, WrappedLogger

from relay.config import Settings
from relay.telemetry.redaction import MASK, is_sensitive_key, redact_value
from relay.telemetry.tracing import current_trace_id

request_id: ContextVar[str | None] = ContextVar("request_id", default=None)

_RESERVED = frozenset({"timestamp", "level", "logger", "trace_id", "request_id"})


def add_ids(_logger: WrappedLogger, _method: str, event: EventDict) -> EventDict:
    trace_id = current_trace_id()
    if trace_id:
        event["trace_id"] = trace_id
    current = request_id.get()
    if current:
        event["request_id"] = current
    return event


def redact(_logger: WrappedLogger, _method: str, event: EventDict) -> EventDict:
    for key, value in list(event.items()):
        if key in _RESERVED:
            continue
        event[key] = MASK if is_sensitive_key(key) else redact_value(value)
    return event


def configure_logging(settings: Settings) -> None:
    """Send every log record to stderr as redacted JSON. Safe to call more than once."""
    shared: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        add_ids,
    ]
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.format_exc_info,
            redact,
            structlog.processors.JSONRenderer(),
        ],
    )
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(settings.log_level)
    # uvicorn installs its own handlers; let its records reach ours instead.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logging.getLogger(name).handlers = []
        logging.getLogger(name).propagate = True
    for name in ("httpx", "httpx2", "mcp", "anthropic", "openai"):
        logging.getLogger(name).setLevel(max(logging.WARNING, root.level))
    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )
