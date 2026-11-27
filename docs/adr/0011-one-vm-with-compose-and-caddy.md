# ADR-0011: Deployment on one VM with Compose and Caddy

- **Status:** Accepted
- **Date:** 2026-11-27

## Context

The public demo needs Postgres with pgvector, Redis, Phoenix, the API, the MCP server, the web app and a TLS proxy. Traffic is a few reviewers at a time, and the whole stack already runs from one Compose file in development and CI. Managed platforms (Fly.io, Railway) would split it into separately priced databases and services, each with its own configuration, and the MCP server and Phoenix would need extra work to stay private.

## Decision

- **One Hetzner Cloud VM** (4 GB RAM, Ubuntu LTS, Docker Engine with Compose), running `compose.prod.yaml`. The firewall opens SSH to the operator's address only, and ports 80 and 443 to everyone.
- **Images from GHCR, built by CI.** Pushing a tag `vX.Y.Z` builds `relay-backend` and `relay-web` (`.github/workflows/release.yml`). The VM never builds anything. Service images (Postgres, Redis, Phoenix, Caddy) and base images are pinned by digest; Dependabot proposes updates.
- **Caddy** terminates TLS with automatic Let's Encrypt certificates, adds security headers (HSTS, a same-origin CSP, no framing), limits request bodies at the edge, and proxies `/api/*` to the API without buffering, for SSE.
- **Phoenix stays private:** published on the VM's loopback only and reached through an SSH tunnel, with its own login off. This replaces ADR-0010's plan to put Phoenix behind authentication with an exporter API key, which would have needed a key created in Phoenix's UI before the exporters could start.
- **Least privilege in the database:** the API and the MCP server connect as `relay_app`, which has data access but no DDL and can only append to the audit log and the transcript. The one-shot `migrate` job runs as the owner: migrations, the grants, the seed and the nightly reset.
- **Deploys** are `deploy/deploy.sh <ssh-target> <version>`: copy the compose file and its configs, set `RELAY_VERSION`, pull, `up --wait`, then a smoke check of `/api/readyz` over HTTPS. A rollback is a deploy of the previous version.
- **The nightly reset** is a systemd timer on the VM (04:00 UTC) that runs `relay demo reset --yes` in the migrate container.

## Consequences

- One machine is a single point of failure, and there is no zero-downtime deploy: `up` restarts changed containers, which drops open SSE streams (clients reconnect and resume). For a demo, that is acceptable.
- Backups are the operator's job (`pg_dump` from the VM, documented in docs/DEPLOY.md); a nightly-reset demo has little to lose.
- Migrations only move forward. Rolling back across a migration needs a database restore, which is documented.
- The same Compose definitions run locally, in CI and in production, so a production problem can be reproduced with `compose.prod.yaml` on a laptop: Phase 8 ran the whole production stack locally on `https://localhost` and passed the browser test against it.
- The reset takes about a minute, during which requests wait on its locks.
