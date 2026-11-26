"""The agent runtime (PLAN.md §5): a hand-written turn loop on the model provider's API.

- `loop.run_turn` runs one customer turn: drain the inbox, call the model, run tools over MCP,
  repeat until the model answers or a safeguard stops it, then finalize.
- `store` keeps the append-only transcript and everything about each turn in Postgres.
- `providers` turn a transcript into provider requests (Anthropic, OpenAI, or a scripted fake).
- `tools` connects to the MCP server per turn and runs the model's tool calls.
- `prompts` and `guardrails` hold the versioned system prompt and the input and output checks.
"""
