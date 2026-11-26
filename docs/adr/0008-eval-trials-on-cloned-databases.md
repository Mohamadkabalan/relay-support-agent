# ADR-0008: Eval trials run on cloned template databases with their own MCP app

- **Status:** Accepted
- **Date:** 2026-11-26

## Context

Each eval trial is graded on the database it leaves behind (PLAN.md §9.3). Trials must not see each other's writes, and they run 8 at a time. Seeding a database per trial, with 500 customers, 2,000 orders and the help center, takes tens of seconds; truncating and reseeding a shared one would serialize the trials. The agent also has to reach the tools the way production does: Streamable HTTP with a per-turn JWT, ownership checks and confirm-then-commit enforced by the server.

## Decision

- **One template, cloned per trial.** It holds the migrations, the base seed (seed 42 on the frozen clock 2026-09-15 15:00 New York) and the help center with its embeddings (`relay/evals/databases.py`).
  - Its name carries its kind and a fingerprint of everything that goes into it: the migration head, the seed generator, the help articles, the policy and the embedder. A run reuses it until one of those changes.
  - It is built under a temporary name and renamed when complete.
  - Each trial gets `CREATE DATABASE ... TEMPLATE` and drops the copy afterwards. The scenario's fixtures are applied on top, in the `PV-9xxxxx` number range the seed leaves free.
- **The MCP app runs in process for each trial.** Each trial builds the production MCP app (`create_http_app`) bound to its own database. The agent reaches that app with `HttpTools`: the same Streamable HTTP client, a JWT per turn and the same verifier.
  - The transport is `httpx2.ASGITransport` instead of a loopback port. The plan said loopback port; the change avoids allocating and cleaning up 8 ports, and it exercises the same app and protocol.
- **The policy re-check uses a second clone.** It runs only when a trial wrote something: a fresh clone gets the same fixtures again, and the policy engine re-plans each committed write on that pre-trial state. A write the policy denies is violation V1.
- **Runs live on the host, not in a container.** Harness runs use `uv run` and the compose Postgres, like the tests, so reports land in the repository directly. The self-tests use the hashing embedder, so CI needs no model download.

## Consequences

- **Clones are cheap.** `make eval-selftest` measures them: on the development machine, cloning the full template took 382 ms on average, and a whole trial with a no-model agent took 2.7 s (23 trials, 2026-11-26).
- **Isolation is total.** A trial can be resumed, retried or inspected afterwards without touching any other.
- **Postgres refuses to clone a template while something is connected to it.** The harness never connects to the template after building it, and it serializes clones with a lock.
- **Templates take disk space.** A new fingerprint drops the older template of the same kind; templates of other kinds, such as the small one the tests build, are left alone.
