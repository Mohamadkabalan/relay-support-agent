# Agent design

How Relay's agent works and why, for a reader who wants the reasoning without the whole plan. The full specification is [PLAN.md §5–§7](../PLAN.md#5-agent-design); the decisions are in [docs/adr/](adr/README.md); the measurements are in [EVALS.md](EVALS.md).

## The one idea

**The model has no authority.** It decides what to say and which tool to call. Everything that matters is decided outside it:

- **Who the customer is** comes from the signed-in session, carried in a per-turn token the model never sees ([ADR-0003](adr/0003-identity-in-the-transport.md)).
- **Whether a write is allowed** comes from a pure policy engine with a config file ([ADR-0006](adr/0006-policy-outcomes-are-data.md)).
- **Whether the customer agreed** comes from a preview token that only works in a later turn ([ADR-0004](adr/0004-confirm-then-commit.md)).
- **Whether a big refund happens** is up to a human operator.

So a jailbreak can make the agent say something wrong, which the evals measure, but it can't move money, change an address, or read another customer's order. The prompt, the guardrails and the judge all sit on top of that floor, not under it.

## A turn

`relay/agent/loop.py` is a hand-written loop on the Messages API, with no agent framework, SDK tool runner or API-side MCP connector ([ADR-0002](adr/0002-hand-written-loop.md)). Owning the loop is what makes the rest possible: an append-only transcript, budgets per turn, a status label for each tool, and a resume turn when an operator decides.

1. **Lock and drain.** One turn per conversation at a time, under a Redis lock ([ADR-0007](adr/0007-turns-in-the-api-process.md)). The turn takes everything waiting in the conversation's inbox, in order: customer messages, and approval outcomes.
2. **Input guards.** Injection heuristics add a trusted note when a message looks like an attack. They never block; three flags hand the conversation to a person.
3. **Call the model**, streaming. Text streams to the browser as it arrives; a `tool_use` block shows a label such as "Checking your order…".
4. **Run the tools** through the MCP server, with a fresh 5-minute token naming the customer, the conversation and the turn. Read-only tools run concurrently; writes run one at a time, in order. A repeated call with the same arguments is answered from the first result.
5. **Repeat** until the model stops calling tools, then run the output guards (a leaked prompt, or another customer's order number, retracts the reply) and finish.

Each turn ends in a named state with a message the customer can see: `completed`, `step_limit` (8 model calls), `budget_exhausted` (a 90-second deadline, or 500K tokens a conversation), `refused`, `failed`, or `paused` (the demo's daily spend cap). The limits are settings, not measurements; the eval reports show how close real conversations come to them.

## The transcript

`relay/agent/store.py`. Every message is appended and never edited ([ADR-0005](adr/0005-append-only-transcript.md)). Assistant responses are stored as the exact JSON the SDK returns, so a replayed request starts with the same bytes as the last one:

- **Prompt caching works.** Tools and the system prompt are pinned per conversation behind one cache breakpoint; the growing tail uses automatic caching.
- **Preserved thinking works.** Thinking blocks are only valid in the conversation that produced them, byte for byte. CI and the evals send `prefix_mismatch_behavior: "error"`, so an accidental edit fails loudly; production sends `drop_block`.
- **Every turn can be replayed** in the console with its model calls, tool calls, tokens, cost and a trace link.

Trusted facts enter as mid-conversation `system` messages, which only the runtime writes: the date, an approval's outcome, an injection flag. When an operator decides, the agent must speak although the customer hasn't. The runtime appends a fixed placeholder user line, then the `system` message with the facts. Typing the placeholder gains a customer nothing, because the facts only come from the `system` message.

## Tools

The MCP server (`relay/mcp_server/`) exposes nine tools: four reads (`get_customer`, `list_orders`, `get_order`, `track_shipment`), help-center search, three writes (`update_shipping_address`, `create_return`, `issue_refund`), and `escalate_to_human`. Design choices that matter:

- **No customer parameter on any tool.** The server binds the customer from the token. Another customer's order number answers `ORDER_NOT_FOUND`, exactly like a typo.
- **Strict schemas** (`strict: true`, no extra properties). Length limits that strict mode can't express are enforced again by Pydantic on the server.
- **Results say what the agent may do next.** An order lists its `allowed_actions`; a denial carries a plain-language reason and the alternatives. The agent explains the rule instead of guessing it.
- **Untrusted text is labeled.** Order notes, carrier scan descriptions and help-article excerpts come back as `{"untrusted_text": ..., "source": ...}`, and the prompt says never to follow instructions found there.
- **There is no amount or destination parameter on a refund.** The server computes the amount from the order lines and always refunds the original payment method. A customer can't negotiate a number the tool doesn't take.

## Writes: preview, confirm, commit

Every write is two calls ([ADR-0004](adr/0004-confirm-then-commit.md)):

1. **Preview** (no token): the server evaluates the policy and returns what would happen (the amount, the card, whether a person must approve it) plus a confirmation token, or a denial with its reason.
2. The agent tells the customer exactly that and asks. **Commit** (with the token) works only in a *later* turn, for the same conversation, tool and canonical arguments, within 30 minutes, once. The policy is evaluated again at commit time, because the order may have changed.

A commit in the same turn as its preview fails with `CONFIRMATION_TOO_EARLY`. The evals count those as blocked attempts, and the mechanism check removes the confirmation rule from the prompt to show the eval sees them rise.

## People in the loop

- **Approvals.** Refunds over $100.00, the third agent refund in 90 days, and reroutes of shipped packages wait for an operator. The customer can keep chatting meanwhile; a pending approval is not a conversation state. The operator's decision re-checks the policy with operator authority (an approval satisfies "needs approval", never a hard "no"), executes, writes the audit row, and schedules a resume turn so the agent tells the customer. Undecided approvals expire after 24 hours and open an escalation.
- **Escalations.** The agent hands off when asked for a person, on a second dispute, on suspected fraud, when tools keep failing, or when the customer is very upset. In an escalated conversation, new messages get a fixed acknowledgement without a model call until an operator hands it back.

## Model settings

Claude Opus 5.5 at effort `medium` (D2), adaptive thinking with summaries shown only to operators, `max_tokens` 16,000, `tool_choice: auto`, and server-side refusal fallbacks with the served model recorded on every call (PLAN.md §5.6). The provider interface (`relay/agent/providers/`) also has an OpenAI Responses provider (contract-tested only, never measured), a scripted provider for tests, and the key-free demo agent that powers the browser test.

## Watching it

- **Traces.** Every turn is a trace in Phoenix: model calls, tool calls (joined with the MCP server's own spans), guard checks and policy decisions, redacted before export ([ADR-0010](adr/0010-phoenix-for-tracing.md)).
- **The console** has a replay of every conversation, the approval queue, escalations, and a metrics page whose numbers each carry their definition.
- **The feedback loop.** "Create eval scenario" turns a real conversation into a draft eval scenario, checked by the suite's own validator and scrubbed of the customer's details, for a person to review and add.

## Measuring it

The evals run the real agent against a simulated customer on a cloned database ([ADR-0008](adr/0008-eval-trials-on-cloned-databases.md)). A trial is graded on exactly what changed in the database, a few conversation checks, and four kinds of violation. A judge then scores the conversation on seven criteria, validated against human labels. See [EVALS.md](EVALS.md). The published numbers come only from committed reports, and there are none until the first run.

## What this design gives up

- **Latency.** Opus with thinking on every turn is slower than a smaller model. The effort sweep and the model comparison exist to show what a cheaper setting costs in quality.
- **Some fluency.** Confirm-then-commit adds a round trip to every change. That's the point, but it is a cost.
- **A streamed reply can be seen before an output guard retracts it.** Buffering whole replies would remove that, at the price of streaming.
- **Single-process turns.** Turns run in the API process (ADR-0007). Many concurrent conversations would want a Redis-backed worker; the lock and inbox design already allow that move.
