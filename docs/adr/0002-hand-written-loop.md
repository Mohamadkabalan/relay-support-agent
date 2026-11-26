# ADR-0002: A hand-written loop on the Messages API

- **Status:** Accepted
- **Date:** 2026-11-26

## Context

The agent needs a loop: call the model, run the tools it asks for, send the results back, and repeat until it answers. Three ready-made options exist:

- an agent framework (LangGraph, the Agent SDK, and so on);
- the Anthropic SDK's tool runner, which runs that loop for you;
- the API-side MCP connector, which lets the API call our MCP server directly.

Each hides decisions this project has to make and show:

- **Preserved thinking.** Every request must replay earlier turns byte for byte (ADR-0005), and the transcript has to stay valid after every failure.
- **Trusted facts.** They arrive as mid-conversation `system` messages at exact positions.
- **Identity.** Each turn gets its own JWT, which only our process can mint (ADR-0003).
- **Safeguards.** Step and token budgets, deadlines, duplicate-call handling, and read-only calls run in parallel.
- **Records.** Every model call and tool call is stored with its usage and cost.

The API-side connector would also put our MCP server on the public internet, behind the API's own authentication instead of our turn tokens.

## Decision

The loop is our own code on the Messages API (`backend/src/relay/agent/loop.py`, about 670 lines with its docstrings), streamed through `client.beta.messages.stream`. The pieces it relies on stay small and separately tested:

- `providers/`: one interface (`render`, `complete`) with Claude, OpenAI and scripted implementations;
- `tools.py`: the MCP client, with timeouts, one retry, dedupe, and scheduling from the tool annotations;
- `store.py`: the append-only transcript and the record of each turn;
- `guardrails/` and `prompts/`.

The model's own text between tool calls, its thinking summaries and its tool calls are stored as returned. Everything the runtime adds (customer messages, tool results, trusted notes, handoff texts) goes through `ModelProvider.render` once, when it is appended.

## Consequences

- **It's all inspectable.** Every invariant a reviewer might ask about is in one file and has a test that names it:
  - every terminal state;
  - recovery after a tool error, and duplicate short-circuiting;
  - ordering of parallel reads;
  - commit across turns, and resuming after an approval;
  - budgets, refusals and `max_tokens`;
  - byte-prefix replay through the real SDK.
- **The tests need no API key.** The scripted provider renders payloads in the Claude format, so tests with it exercise the same replay rules as the live provider.
- **We maintain what a framework would maintain:**
  - streaming event handling;
  - the server-side fallback echo rules (which blocks are dropped before a `fallback` block);
  - cost accounting across fallback attempts.

  It stays inside `providers/anthropic.py` and its contract tests.
- **Switching provider means a new conversation.** Each conversation is pinned to its provider (§5.11). The OpenAI provider exists to show the interface holds; it is contract-tested against a fake API only.
