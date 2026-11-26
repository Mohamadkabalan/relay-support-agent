# ADR-0005: Append-only transcript with raw provider payloads

- **Status:** Accepted
- **Date:** 2026-11-26

## Context

Claude Opus 5.5 binds each thinking block to the conversation that produced it. A later request whose earlier history differs in any byte has those blocks rejected (400) or dropped, depending on `thinking.block_binding.prefix_mismatch_behavior`. Accounts created on or after 2026-08-31 are enforced by default. The prompt cache is a prefix match for the same reason. Any history edit therefore costs reasoning, cache hits, or a failed request:

- a rebuilt system prompt;
- a re-serialized tool result;
- a deleted reminder;
- a JSON column that reorders keys.

## Decision

- **Pinned per conversation.** The provider, model, effort, exact system-prompt text, prompt version and hash, tool manifest (JSON text) and policy version are stored on `conversations` when it is created and never changed. Every request renders `tools` from the stored manifest text.
- **Rendered once.** Each transcript entry is stored in `messages.raw` as the exact JSON text sent to the provider:
  - the model's replies are stored as the SDK echoes them (`model_dump(exclude_unset=True, by_alias=True)`, the form the SDK uses when you pass a response back);
  - everything else is rendered once by the provider when it is appended.

  A request's `messages` are the stored payloads concatenated in order.
- **Text, not `jsonb`.** `raw` and `tool_manifest` are `text` columns with an `IS JSON` check. `jsonb` would reorder keys, and nothing ever queries inside them. `messages.content` (`jsonb`) holds the same entry in a provider-neutral form for the console.
- **Append-only, enforced.** A statement-level trigger rejects UPDATE and DELETE on `messages`, like `audit_log`.
- **Trusted facts are appended, never inserted.** They are `role: "system"` messages placed after a user message: today's date and the customer, an injection notice, an operator decision, a wrap-up warning, a retraction.
  - When a turn ends without a model reply (handoff, refusal, failure, acknowledgement), the runtime appends what the customer saw as an assistant message. Otherwise the next customer message would follow a system note, which the API rejects.
  - A notice the model must not see (a retraction's replacement text, the length-limit close) is stored with `raw` NULL.
- **Interrupted tool calls still get results.** A reply that asked for tools always gets results appended, even when the turn ended first; those results are marked `[INTERRUPTED]`.
- **Truncated or refused output is never appended.** A `max_tokens` response is retried once with more room. A refusal ends the turn. Either way, the stored model call keeps the raw stop reason.
- **Mismatch behavior is set explicitly.** Development, CI, evals and `make smoke` send `prefix_mismatch_behavior: "error"` (`RELAY_THINKING_PREFIX_MISMATCH`), so an accidental edit fails loudly. Production will send `"drop_block"` and alert on reported drops (Phase 8).

## Consequences

- Tests check that each request starts with exactly the previous request's messages, system prompt and tools:
  - with the scripted provider after every loop test;
  - through the real Anthropic SDK on the HTTP bodies it sent (`tests/integration/agent/test_replay.py`).
- **A history fix is a new conversation.** If a transcript is wrong, it can't be patched in place.
- **Retries happen by resending.** A failed turn isn't re-run on the same transcript; the apology asks the customer to send the message again.
- **Old prompts stay readable.** The prompt text is stored per conversation, so old conversations replay their own prompt after a deploy changes it.
- **`conversation_id` on store, approval and audit rows stays a plain UUID, not a foreign key.**
  - The audit trail must outlive a conversation: the demo reset deletes conversations, and audit rows can't be changed.
  - The MCP server records whatever conversation the API's signed token names.
