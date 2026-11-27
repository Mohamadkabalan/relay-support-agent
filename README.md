# Relay

A customer-support agent for Pixel & Volt, a fictional online electronics store. Customers chat with it to check orders, track packages, change shipping addresses, start returns and request refunds. The agent acts only through tools on an MCP server, risky actions wait for a human operator, and an eval suite measures the whole system.

> **Status: Phase 7 of 8 (the full eval suite), built but not yet run against the model.** The store, its policy engine, its seed data, the nine MCP tools, the agent and the web app work. The eval suite has 80 scenarios (52 regular, 28 red team), a judge with a labeling and agreement workflow, failure triage, and a CI gate; all of it passes its self-tests without a model. No eval run with the model has happened yet, so there are no eval numbers.

## Run it locally

You need Docker with Compose v2, GNU Make, [uv](https://docs.astral.sh/uv/) and Node.js 24 or newer.

```bash
make up      # builds the images, migrates the database, waits until every service is healthy
make seed    # 500 customers, ~2,000 orders, 6 demo personas, 30 help articles; checks itself
make down    # stops it (make clean also deletes the data volumes)
```

| URL | What |
|---|---|
| http://localhost:8080 | The app, through Caddy (`/api/*` goes to the API) |
| http://localhost:8080/api/readyz | API readiness: Postgres and Redis checks |
| http://localhost:6006 | Phoenix, the trace UI |

`make up` creates `.env` from `.env.example` the first time. Only `make chat`, `make smoke` and the live eval targets need an API key (`ANTHROPIC_API_KEY` in `.env`); the tests and `make eval-selftest` don't.

`make seed` is idempotent: run it again and it reports the data is already there (`RESET=1` starts over, `SEED=7` picks another seed). It prints the order distribution, a sample of orders and a data hash, and fails if an invariant or a demo persona's expected policy outcome doesn't hold. The same seed and clock always give the same hash:

```bash
docker compose run --rm migrate relay seed --reset --now 2026-09-15T15:00:00-04:00
```

## Drive the tools yourself

The agent's nine tools live on Relay's own MCP server. Every tool acts for the customer the connection names, never for an ID in its arguments, and writes go through preview-then-confirm with policy checked on the server. [docs/mcp.md](docs/mcp.md) shows how to connect **Claude Desktop** (a demo script included), open the **MCP Inspector** (`make mcp-inspect`), or call a tool from the shell:

```bash
make mcp-call TOOL=get_order ARGS='{"order_number": "PV-800101"}'   # as Dana, a demo customer
```

## Talk to the agent

The agent is a hand-written loop on the Claude Messages API ([ADR-0002](docs/adr/0002-hand-written-loop.md)) that acts only through the MCP tools, with a fresh signed token per turn. With `ANTHROPIC_API_KEY` in `.env` and the stack running:

```bash
make chat                                         # as Dana: her $349.99 headphones arrived cracked
make chat MCP_CUSTOMER=sam.castillo@example.com   # a package with no scan for 9 days
make smoke                                        # one live refund conversation, checked end to end
```

`make chat` prints what the customer sees, the status labels, approvals and escalations, and each turn's tokens and cost. Writes it makes are real rows in the demo database; `make seed RESET=1` puts everything back. `make smoke` runs on a throwaway database. It costs a few cents and checks three things: the refund was issued, no turn failed, and the second model call read the prompt cache.

The transcript is append-only and replayed byte for byte ([ADR-0005](docs/adr/0005-append-only-transcript.md)). The system prompt is in [backend/src/relay/agent/prompts/v1.md](backend/src/relay/agent/prompts/v1.md).

## The web app

Open http://localhost:8080, pick one of the six demo customers and open the chat. Then open the operator console (link at the top) in another tab of the same browser: in demo mode it shows only your own demo customers.

- **The chat** streams the agent's replies and shows what it is doing ("Checking your order…"), approval cards that update when an operator decides, and thumbs up or down. It reconnects by itself.
- **The console** has live conversations, the approval queue with a decision drawer, escalations (reply, resolve, hand back), a metrics dashboard, and a replay of every conversation: each turn's model and tool calls, policy decisions, tokens, cost and a link to its trace in Phoenix. "Create eval scenario" drafts an eval scenario from a conversation (needs a key).

No API key? Set `RELAY_PROVIDER=scripted` in `.env` and `make up`: a rule-based demo agent then tells the damaged-headphones refund story through the real tools, approvals and events, without calling a model.

```bash
make e2e     # the browser smoke test (Playwright, 375 px and desktop) on the key-free demo agent
```

## The HTTP API

The API serves the chat and the operator console behind Caddy at http://localhost:8080/api (schema at `/api/docs`). In demo mode, the default in `.env.example`, each browser gets private copies of the six demo personas, and a demo operator login sees only that browser's conversations. With `ANTHROPIC_API_KEY` in `.env`:

```bash
make approval-flow   # with curl: Dana's $349.99 refund, the operator's approval, the agent's follow-up
```

Replies stream over server-sent events that resume after a reconnect. Turns run under a per-conversation lock, so a message sent mid-turn gets its own turn next ([ADR-0007](docs/adr/0007-turns-in-the-api-process.md)). A daily spend cap stops model calls before they start. Every turn is a trace in Phoenix (http://localhost:6006), grouped by conversation and redacted before it leaves the process ([ADR-0010](docs/adr/0010-phoenix-for-tracing.md)). [docs/api.md](docs/api.md) lists the routes, the events and the limits.

## Evals

Each trial is a simulated conversation: a model plays the customer from a short brief, the real agent answers through the real MCP tools, and an operator stub decides any approval. The trial runs on its own copy of a seeded template database. It passes when the database changes are exactly what the scenario expects, its conversation checks pass, and no violation occurred: a write the policy forbids, another customer's data, a prompt leak, or an action the agent claimed but never took. A judge model then scores the conversation on seven yes-or-no criteria, reported apart from task success. [evals/SCENARIOS.md](evals/SCENARIOS.md) lists the 80 scenarios; [docs/EVALS.md](docs/EVALS.md) explains the method, and [ADR-0008](docs/adr/0008-eval-trials-on-cloned-databases.md) the setup.

<!-- relay:results:start -->
No run has been published yet. `make eval-publish RUN=evals/reports/<run>` writes this table from a committed report.
<!-- relay:results:end -->

```bash
make eval-selftest          # no model calls: schema, policy agreement, oracle, null agent, errors
make eval                   # all 80 scenarios, 3 trials each, judged; prints a cost estimate and asks first
make eval SUBSET=ci K=1     # the 10-scenario CI subset, one trial each
make eval-gate              # what CI runs: the CI subset with retries, against evals/baseline.json
make eval-sweep             # agent effort low and high (medium is the main run)
make eval-models            # Claude Sonnet 5.5 and Claude Haiku 5.5 as the agent
make eval-judge-check       # known-bad replies the judge must fail
```

A run writes `evals/reports/<UTC time>-<git sha>/report.md` and `report.json` (configuration, every graded row, aggregates with 95% intervals). Transcripts stay local. `make eval-triage`, `make eval-label`, `make eval-agreement` and `make eval-publish` turn a run into the published numbers; docs/EVALS.md has the workflow. No number here or in EVALS.md is typed by hand.

## Help-center search

`make eval-retrieval` scores help-center search on 40 labeled questions and writes `evals/retrieval/report.md`; the result chose vector search ([ADR-0009](docs/adr/0009-retrieval-vector-search.md)).

## Develop

```bash
make dev     # the same stack with hot reload for the backend and the frontend
make lint    # ruff, mypy --strict, ESLint, tsc
make test    # backend tests with the 90% domain coverage gate; starts Postgres and Redis
make hooks   # installs the pre-commit hooks
make help    # every target
```

## Layout

```text
backend/    Python package `relay`: API (FastAPI), MCP server, CLI, tests
            relay/domain: policy engine (policy.yaml) and the store services
            relay/mcp_server: the nine tools, identity, strict schemas
            relay/agent: turn loop, scheduler, transcript store, providers, prompt, guardrails
            relay/api: sessions, rate limits, chat, console, SSE; relay/telemetry: traces, logs
            relay/evals: eval harness (template DB, simulator, graders, judge, runner,
            reports, labels, CI gate, publishing)
            relay/db: models and migrations; relay/seed: demo data; relay/kb: help center
frontend/   Next.js app: store page, chat widget, operator console; lib/api: typed client; e2e/
deploy/     Caddy and Postgres configuration
docs/       EVALS.md (method, results), mcp.md (Claude Desktop, Inspector), api.md (routes,
            events, limits), examples/,
            adr/ (architecture decision records)
evals/      Scenarios (scenarios/, redteam/, SCENARIOS.md), run reports, labels/, baseline.json
            (after the first full run), retrieval eval
```
