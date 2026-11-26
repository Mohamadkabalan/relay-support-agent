"""PII redaction for everything that leaves the process as telemetry: spans and logs (§5.12).

Postgres keeps the full transcripts, readable by operators only. Traces and logs get copies with:

- known PII fields masked by key (names, emails, phone numbers, address lines, card digits);
- free text masked by pattern: emails, phone numbers, card-like numbers and street addresses.

The patterns are deliberately broad. A masked order total in a log line costs nothing; a leaked
street address in a third-party trace store is the failure this module exists to prevent.
"""

import re
from collections.abc import Mapping, Sequence
from typing import Any

MASK = "[redacted]"

# The last segment of an attribute or log key ("customer.email" -> "email").
SENSITIVE_KEYS = frozenset(
    {
        "email",
        "phone",
        "full_name",
        "customer_name",
        "recipient_name",
        "display_name",
        "line1",
        "line2",
        "address",
        "street",
        "postal_code",
        "payment_last4",
        "last4",
        "password",
        "authorization",
        "cookie",
    }
)

_STREET_SUFFIX = (
    r"street|st|avenue|ave|road|rd|boulevard|blvd|lane|ln|drive|dr|court|ct|place|pl|way|"
    r"terrace|ter|parkway|pkwy|highway|hwy|circle|cir|square|sq|trail|trl"
)

PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"), "[email]"),
    # Card-like: 13 to 19 digits, optionally grouped by spaces or dashes.
    (re.compile(r"(?<![\w-])\d(?:[ -]?\d){12,18}(?![\w-])"), "[card]"),
    # North American phone numbers, with or without +1, brackets, dots, dashes or spaces.
    (
        re.compile(r"(?<![\w-])(?:\+?1[ .-]?)?\(?\d{3}\)?[ .-]?\d{3}[ .-]?\d{4}(?![\w-])"),
        "[phone]",
    ),
    # A house number, one to four words, and a street suffix: "418 Maple Avenue", "77 Prospect Pl".
    (
        re.compile(
            rf"\b\d{{1,6}}(?:\s+[A-Za-z0-9.'-]+){{1,4}}?\s+(?:{_STREET_SUFFIX})\b\.?",
            re.IGNORECASE,
        ),
        "[address]",
    ),
    (re.compile(r"\b(?:apt|apartment|unit|suite|ste)\.?\s*#?\s*\w+\b", re.IGNORECASE), "[unit]"),
    # A state code and a ZIP code: "OH 43215", "WA, 98101-1234".
    (re.compile(r"\b[A-Z]{2},?\s+\d{5}(?:-\d{4})?\b"), "[postal]"),
)


def redact_text(text: str) -> str:
    for pattern, mask in PATTERNS:
        text = pattern.sub(mask, text)
    return text


def is_sensitive_key(key: str) -> bool:
    return key.rsplit(".", 1)[-1].lower() in SENSITIVE_KEYS


def redact_value(value: Any) -> Any:
    """Mask PII inside a string, or inside the strings of a list, tuple or dict."""
    match value:
        case str():
            return redact_text(value)
        case Mapping():
            return redact_mapping(value)
        case list() | tuple():
            return type(value)(redact_value(item) for item in value)
        case _:
            return value


def redact_mapping(values: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: MASK if is_sensitive_key(key) else redact_value(value) for key, value in values.items()
    }


def redact_sequence(values: Sequence[Any]) -> list[Any]:
    return [redact_value(value) for value in values]
