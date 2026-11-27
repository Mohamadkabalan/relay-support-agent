# Security

Relay's threat model, the controls that answer each threat, how each control is checked, and what is left open. The design rules are in [PLAN.md §11](../PLAN.md#11-security); this page is the reviewer's version.

**Reporting a problem:** please use GitHub's private vulnerability reporting on this repository ("Report a vulnerability" under the Security tab) rather than a public issue.

## What we protect

- **Customers' data:** names, emails, phone numbers, addresses, orders, conversations.
- **Money and goods:** refunds, return labels, address changes. A wrong write costs the store directly.
- **The operator's authority:** approvals and escalations, which can unlock writes the agent can't make.
- **Secrets:** the Anthropic API key, the session and turn-token keys, database passwords.
- **The spend budget** of the public demo.

## Who we defend against

- **A customer, or an attacker posing as one**, who chats with the agent and tries to get refunds they aren't owed, another customer's data, or the system prompt. They control their messages, and some free text the agent will read: their own order notes and delivery instructions.
- **Third-party text:** carrier scan descriptions and help-article bodies can carry planted instructions.
- **An anonymous internet client** hitting the API directly: no session, a stolen cookie, cross-site requests, floods.
- **A compromised dependency or image** (supply chain).

The model itself is treated as untrusted with respect to authority: it can be talked into anything, so nothing depends on it refusing.

## Trust boundaries

| Input | Trusted? | Handling |
|---|---|---|
| Customer messages | No | Size and rate limits, injection heuristics. Never parsed for authority. |
| Tool arguments written by the model | No | Validated by Pydantic in the MCP server. Identity never comes from arguments. |
| Order notes, carrier event text, help-article bodies | No | Returned inside `untrusted_text` fields; the system prompt says these are data. |
| The per-turn JWT naming the customer, conversation and turn | Yes | Minted by the API from the session; verified by the MCP server (audience, expiry, signature). |
| Mid-conversation `system` messages | Yes | Written only by our runtime: the date, approval outcomes, injection flags. |
| Operator actions | Yes, and audited | Require an operator session. Every write records who, what and why. |

## Threats and controls

### The agent is talked into a write it shouldn't make

The model holds no authority. Every control below is in code, outside the model:

- **Identity is in the transport, not the arguments** ([ADR-0003](adr/0003-identity-in-the-transport.md)). The MCP server learns the customer from the turn token. There is no customer parameter to forge, and another customer's order answers `ORDER_NOT_FOUND`, the same as a nonexistent one.
- **The policy engine decides every write** ([ADR-0006](adr/0006-policy-outcomes-are-data.md)): windows, limits, ownership, quantities, the velocity rule. Refund amounts and destinations have no parameter at all: the server computes the amount from the order and always pays the original method.
- **Confirm-then-commit** ([ADR-0004](adr/0004-confirm-then-commit.md)): a write needs a preview token from an earlier turn, for the same conversation, tool and arguments, unexpired and unused. A model can't preview and commit in one breath.
- **Refunds over $100.00, and the third refund in 90 days, wait for an operator**, and the policy is checked again when the operator approves.
- **Checked by:** the policy tests (table-driven boundaries plus Hypothesis properties: total refunded never exceeds total paid), the MCP server tests, and the eval suite's 28 red-team scenarios. The eval re-plans every committed write on the pre-trial state and counts any write the policy forbids as a critical violation (V1). No eval run against the model has happened yet (docs/EVALS.md).

### Another customer's data leaks

- Ownership checks in every tool (above).
- An output guard retracts a reply that names an order number not belonging to the customer.
- **Checked by:** MCP tests that call every tool with a foreign order, and eval violation V2 (another customer's name, email, order number or street in any reply).

### Prompt injection through text the agent reads

- Free text from customers, carriers and article authors is wrapped as `untrusted_text`, and the prompt says to treat it as data.
- Injection heuristics flag messages (override attempts, fake system or tool output, encoded payloads). A flag never blocks; three flags escalate the conversation.
- The structural controls above mean a successful injection still can't move money or data the policy doesn't allow.
- **Checked by:** red-team scenarios with instructions planted in an order note (3), a help article (2) and a carrier scan (2), and obfuscated ones (base64, homoglyphs, zero-width characters).

### The system prompt leaks

The prompt holds nothing secret, but it shouldn't be dumped. A canary string, or 30 or more consecutive words of the prompt, in a reply makes the output guard retract it. The eval counts it as V3. Streaming means the customer may see the text briefly before the retraction; the stored transcript keeps the original for review.

### Sessions and the console are abused

- **Sessions:** opaque IDs in HttpOnly, Secure, SameSite=Lax cookies; the data lives in Redis under a TTL, keyed by an HMAC of the ID, so a Redis dump holds no usable session. State-changing requests must carry an allowed `Origin`.
- **Operators:** an Argon2 password hash from the environment, and in demo mode a demo operator login that sees only its own visitor's data. Customer sessions get 403 on every console route.
- **Rate limits:** chat writes per IP (30 a minute), messages per visitor (8 a minute, 120 a day), logins per IP (10 a minute), operator requests per operator (120 a minute).
- **Size limits:** 16 KiB request bodies at the API (1 MB at Caddy), 2,000-character messages, 40 messages per conversation, length limits on every tool argument.
- **Checked by:** the API tests (CSRF origin checks, 401 and 403 on the console, limits returning 429 and 413) and CI's stack job, which signs in a demo customer through Caddy and checks the console refuses it.

### The audit trail is altered

Every write records the actor type and ID, the customer acted for, the action, the entity, before and after values, the reason, the approval, the conversation and the trace. The API and the MCP server connect as `relay_app`, which can only SELECT and INSERT on `audit_log` and on the transcript (`messages`); triggers also refuse UPDATE and DELETE there. The role has no DDL rights at all. Migrations, the seed and the nightly reset run as the owner in a one-shot job.
**Checked by:** `tests/integration/test_app_role.py` (UPDATE, TRUNCATE, CREATE, DROP and ALTER are refused) and `tests/integration/test_audit_log.py`.

### Secrets leak

- Secrets come only from the environment; `.env` is gitignored and `.env.example` documents each one. In production, settings refuse any secret containing `dev-only`, `RELAY_COOKIE_SECURE` off, and unredacted trace content.
- `compose.prod.yaml` has no defaults for secrets: it refuses to start without them.
- The MCP server never receives the model keys.
- gitleaks runs in pre-commit and, over the whole history, in CI.
- **Logs and traces** are redacted before they leave the process: PII keys are masked, and free text is masked by pattern (emails, phones, card-like numbers, addresses). The Anthropic SDK's spans stay metadata-only. `metadata.user_id` sent to the API is a salted hash. A test exports a PII-heavy conversation and finds none of it in any span.

### The public demo is abused for cost

A hard daily spend cap: before every model call the API reserves the call's worst-case cost in Redis, and when that would pass `RELAY_DAILY_SPEND_CAP_USD` the chat answers "paused until 00:00 UTC" without calling the model. The nightly reset keeps the spend counters. The demo's API key belongs to a dedicated Claude Console workspace with its own spend limit, as a backstop.

### Network exposure

Only Caddy is published (80 and 443), with automatic HTTPS, HSTS, a same-origin Content-Security-Policy, no framing, and `nosniff`. Postgres, Redis, the API and the MCP server are internal. The MCP server needs a turn token on every request and checks the Host header. Phoenix is published on the VM's loopback only and reached through an SSH tunnel. The VM's firewall allows SSH from the operator's address only ([ADR-0011](adr/0011-one-vm-with-compose-and-caddy.md)).

### Containers and the supply chain

- Every service runs as a non-root user (CI checks the two app images), with a read-only root filesystem, `no-new-privileges` and all capabilities dropped. Caddy gets back only `NET_BIND_SERVICE`.
- Base and service images are pinned by digest; `uv.lock` and `package-lock.json` are committed and installed with `--locked` and `npm ci`. Dependabot proposes updates weekly.
- CI runs `pip-audit` on the locked Python dependencies and `npm audit` on the production npm dependencies, and fails on findings. `make audit` runs both locally.

## Audit results (2026-11-27)

| Check | Command | Result |
|---|---|---|
| Python dependencies | `make audit` (pip-audit 2.10.1 on `uv export --no-dev`) | No known vulnerabilities |
| npm dependencies | `make audit` (`npm audit --omit=dev`); also run without `--omit=dev` | 0 vulnerabilities |
| Secrets in the git history | `gitleaks git` (v8.30.1, all 11 commits) | No leaks |
| Secrets in the working tree | `gitleaks dir` | 13 hits, all in `frontend/.next/` build output (gitignored): Next.js's per-build preview and server-action keys. None is in git. |

## Known limitations

- **Next.js build keys in the web image.** Each `next build` generates a preview-mode secret and a server-action encryption key, and the standalone output ships them in the image. Relay uses neither preview (draft) mode nor server actions, so they protect nothing here. If either feature is ever added, set `NEXT_SERVER_ACTIONS_ENCRYPTION_KEY` and stop publishing the image.
- **`'unsafe-inline'` scripts in the CSP.** Next.js hydrates with inline scripts, and a static build has no nonces. The CSP still blocks every third-party origin; React escapes all output, there is no `dangerouslySetInnerHTML`, and assistant text renders as plain text.
- **The injection detector is heuristic.** Its job is monitoring and nudging the model; it is not the defense. Its precision and recall belong in the eval report once a run exists.
- **Retracted replies are visible for a moment** while they stream.
- **One operator account** (plus the demo operator). There are no roles, no multi-factor login, and no per-operator audit review screen beyond the audit rows.
- **No DDoS protection** beyond the rate limits and Caddy; the demo is one VM.
- **Phoenix has no login.** It relies on being reachable only through the SSH tunnel.
- **Stdio MCP (Claude Desktop)** acts for one customer chosen by an environment variable, with no turn context. It is for local use only ([docs/mcp.md](mcp.md)).
- **Backups are manual** (docs/DEPLOY.md); the demo resets nightly anyway.
