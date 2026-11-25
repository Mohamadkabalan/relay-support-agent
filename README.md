# Relay

A customer-support agent for Pixel & Volt, a fictional online electronics store. Customers chat with it to check orders, track packages, change shipping addresses, start returns and request refunds. The agent acts only through tools on an MCP server, risky actions wait for a human operator, and an eval suite measures the whole system.

> **Status: Phase 1 of 8 (store domain, policy engine, data).** The store, its policy engine and its seed data work; there is no agent or UI yet. [PLAN.md](PLAN.md) has the design and the roadmap; this README becomes the full project README in Phase 8.

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

`make up` creates `.env` from `.env.example` the first time. Nothing needs an API key yet.

`make seed` is idempotent: run it again and it reports the data is already there (`RESET=1` starts over, `SEED=7` picks another seed). It prints the order distribution, a sample of orders and a data hash, and fails if an invariant or a demo persona's expected policy outcome doesn't hold. The same seed and clock always give the same hash:

```bash
docker compose run --rm migrate relay seed --reset --now 2026-09-15T15:00:00-04:00
```

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
            relay/db: models and migrations; relay/seed: demo data; relay/kb: help center
frontend/   Next.js app: chat widget and operator console (Phase 6)
deploy/     Caddy and Postgres configuration
docs/adr/   Architecture decision records
evals/      Labeled data and reports (retrieval now; agent evals from Phase 4)
```
