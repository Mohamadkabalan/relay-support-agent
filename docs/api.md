# The HTTP API

The API serves the chat widget and the operator console (Phase 6) from one origin: Caddy sends `/api/*` to FastAPI and everything else to Next.js. The interactive schema is at http://localhost:8080/api/docs, and `/api/openapi.json` is what the frontend's typed client will be generated from.

## Try it

```bash
make up && make seed            # the stack, with ANTHROPIC_API_KEY in .env
make approval-flow              # docs/examples/approval-flow.sh: refund, approval, resume, with curl
```

The script signs in as a private copy of Dana, asks for the $349.99 refund, confirms it, signs the same browser in as the demo operator, approves the request and prints the agent's follow-up and the event stream. Then open Phoenix (http://localhost:6006) and find the conversation's session: one trace per turn, with model, tool, guard and policy spans.

## Sessions

Signing in sets an HttpOnly, SameSite=Lax cookie (`relay_session`, path `/api`) holding a random token; the session lives in Redis under an HMAC of that token for 12 hours, renewed on use. Every login issues a new token. One session can hold a customer and an operator at once, because in the demo the widget and the console share a browser.

| Route | What it does |
|---|---|
| `GET /api/auth/personas` | The six demo personas, for the picker |
| `POST /api/auth/demo` `{"persona": "dana"}` | Demo mode: signs in as this browser's private copy of the persona, made on first use (D4). Otherwise: the seeded persona itself |
| `POST /api/auth/operator` `{"email", "password"}` | The operator, checked against `RELAY_OPERATOR_PASSWORD_HASH` (Argon2). Sees everything |
| `POST /api/auth/demo-operator` | Demo mode only: an operator that sees this browser's customers only |
| `GET /api/auth/session`, `POST /api/auth/logout` | Who is signed in; sign out |

## Customer chat

Every route acts for the session's customer. Someone else's conversation answers 404, exactly like a missing one.

| Route | What it does |
|---|---|
| `POST /api/conversations` | Start a conversation (pins the model, prompt and tool manifest) |
| `GET /api/conversations`, `GET /api/conversations/{id}` | The customer's conversations; one with its visible messages and pending approvals |
| `POST /api/conversations/{id}/messages` `{"text"}` | Queue a message (202). The reply streams over the events endpoint |
| `GET /api/conversations/{id}/events` | Server-sent events, below |
| `POST /api/conversations/{id}/feedback` `{"rating": "up"\|"down"}`, `POST .../close` | Thumbs up or down; end the chat |

Without a model key the chat routes answer 503 and say why; logins and the console still work.

## Operator console

Every route needs an operator session (401 without a session, 403 for a customer session).

| Route | What it does |
|---|---|
| `GET /api/console/conversations` | Live conversations: status, last activity, cost, flags, pending approvals, open escalation |
| `GET /api/console/conversations/{id}` | The replay: every transcript entry, turns with model and tool calls, costs, trace IDs, approvals, escalations |
| `GET /api/console/conversations/{id}/events` | The same event stream, plus console-only events (`guard.input`) |
| `POST /api/console/conversations/{id}/messages` `{"text"}` | Write to the customer of an escalated conversation. The next turn relays it to the agent as a trusted note |
| `GET /api/console/approvals`, `GET .../approvals/{number}` | The queue (pending by default, `?status=` for others) |
| `POST /api/console/approvals/{number}/decision` `{"decision": "approve"\|"reject", "note"}` | One transaction: lock, re-check the policy, execute, audit, queue the resume turn. A rejection needs a note |
| `GET /api/console/escalations`, `POST .../escalations/{number}/resolve` `{"note", "hand_back"}` | Resolve a case, then close the conversation or hand it back to the agent |

Every operator write has an audit row: who, what, for whom, why, and the trace ID.

## Events

`GET .../events` is one long-lived stream per conversation. Stored events carry an `id:`; reconnect with `Last-Event-ID` (EventSource does this itself) or `?after=<id>` to get the ones you missed. Text deltas carry no ID and are never replayed: after a reconnect, `message.completed` has the whole message. A `: ping` comment arrives after 15 idle seconds.

| Event | Data |
|---|---|
| `turn.started`, `turn.completed` | `turn`, and `trigger` or `status` |
| `status` | `label`, e.g. "Checking your order…" |
| `message.delta`, `message.completed` | `text`; completed also has `message_seq`, and `notice`/`author` for messages the runtime or an operator wrote |
| `message.retracted` | `message_seq`, `replacement` |
| `approval.pending`, `approval.resolved` | `approval_number`, and `summary` or `decision` |
| `conversation.escalated`, `conversation.updated` | `escalation_number` and `expected_response`; or `status` |
| `error` | `code` (`turn_failed`, `paused`), `message` (safe to show) |

## Limits

| Limit | Default | Answer |
|---|---|---|
| Request body | 16 KiB | 413 |
| Message length | 2,000 characters | 413, before anything is stored |
| Messages per browser | 8 a minute, 120 a day | 429 with `Retry-After` |
| Chat requests per IP | 30 a minute | 429 |
| Logins per IP | 10 a minute | 429 |
| Operator requests | 120 a minute | 429 |
| Messages waiting for a running turn | 3 | 429 |
| Daily spend | $20 (`RELAY_DAILY_SPEND_CAP_USD`) | The turn ends `paused` with a fixed message; no model call starts |

State-changing requests whose `Origin` isn't in `RELAY_ALLOWED_ORIGINS` get 403.
