# ADR-0004: Confirm-then-commit for write tools

- **Status:** Accepted
- **Date:** 2026-11-25

## Context

The agent can change real records: refunds, returns, shipping addresses. A model can misread a request, act on a hypothetical ("what if I returned it?"), or be talked into acting by an injected instruction. Asking the model to "always confirm first" is a prompt rule, and prompt rules fail some of the time. We wanted the confirmation to be enforced by the server, so that skipping it is impossible rather than unlikely.

## Decision

Every write tool except `escalate_to_human` works in two calls (`relay/domain/previews.py`, `relay/domain/writes.py`):

1. **Preview.** Called without a token, the tool evaluates the policy on fresh facts and returns a summary, the amount the server computed, whether an operator must approve, and a single-use `confirmation_token`. Nothing is written except the preview row.
2. **Commit.** Called again with the token, the tool commits only if the token exists, belongs to this customer and conversation, names the same action with the same canonical arguments, was issued in an *earlier turn*, has not expired (30 minutes) and has not been used. Then it evaluates the policy again, because the order may have changed. If the amount differs from the preview, the commit fails with `CONFLICT` and the customer sees a new preview.

Details that matter:

- The token is bound to the canonical arguments (order, reason, lines, normalized address), not to free text like the customer's description, so rephrasing that text doesn't invalidate a confirmation.
- A used token replays its stored outcome. A retried commit (after a timeout, say) returns the same refund instead of paying twice; a unique index on `refunds.preview_id` backs this up.
- The commit locks the customer row and then the order row, so two commits on the same order run one after the other. `tests/integration/test_concurrency.py` races two commits for the same unit: one completes, the other is denied `ALREADY_REFUNDED`.
- Over stdio (Claude Desktop) there are no turns, so "an earlier turn" can't be checked; Claude Desktop's own tool-approval prompt stands in for it.

## Consequences

- An agent that skips confirmation gets `CONFIRMATION_TOO_EARLY` and has to ask the customer. The eval harness counts these attempts (PLAN.md §9.3), which makes the model's behavior measurable without trusting it.
- Each write costs one extra tool call and one extra model step.
- The summary the customer confirms is written by the server, from the same numbers the commit will use, so the agent can't promise an amount the server won't pay.
