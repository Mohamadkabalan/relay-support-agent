# ADR-0006: Policy outcomes are data; failures are errors

- **Status:** Accepted
- **Date:** 2026-11-25

## Context

A tool call can go wrong in two very different ways. The customer may ask for something the policy doesn't allow (a return on day 31). Or the call itself may be wrong (an order number that isn't on this account, a line that doesn't exist, a stale confirmation token). The agent should handle these differently: explain the first to the customer, with alternatives; fix the second itself and retry, usually without the customer noticing.

## Decision

- The policy engine (`relay/domain/policy.py`) is a pure function, `evaluate(request, facts, config, now) -> Decision`. A decision is `allow`, `needs_approval` or `deny`, with every rule that matched, a plain-language explanation and alternatives. Write tools return it as data: `status: "denied"` with `rule`, `code`, `explanation` and `alternatives`.
- Failures raise a `DomainError` subclass (`relay/domain/errors.py`) with a code, a message and a hint, such as `[ORDER_NOT_FOUND] No order PV-104299 on this account. Ask the customer to check the number, or call list_orders.` The MCP layer (Phase 2) turns these into `ToolError`, which the model sees as `is_error: true`.
- Ownership failures look exactly like missing orders, so an error can't be used to probe for other customers' order numbers.
- Rules run in a fixed order: ownership, input validation, denials (all recorded), approval triggers (all recorded), allow.

## Consequences

- Every policy boundary is a table-driven unit test on hand-built facts, with no database: day 30 against day 31 in the store time zone, $100.00 against $100.01, 6 against 7 days without a scan (`tests/unit/test_policy.py`). Hypothesis properties check that refunds never exceed what was paid across any sequence of requests.
- The read-only `allowed_actions` on `get_order` comes from the same engine, so what the agent is told is possible and what the tools allow can't disagree.
- Tool results and error rates can be measured separately: policy denials are expected outcomes, while errors point at agent mistakes or bugs.
