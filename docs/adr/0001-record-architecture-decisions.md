# ADR-0001: Record architecture decisions

- **Status:** Accepted
- **Date:** 2026-11-22

## Context

Relay makes several choices a reader will want explained, not just listed: why the agent loop is hand-written, why customer identity travels in the transport instead of tool arguments, why evals run on cloned databases. PLAN.md describes the intended design, but a plan drifts as code lands, and it doesn't record what was considered and rejected.

## Decision

Each significant decision gets a short Architecture Decision Record in `docs/adr/`, numbered in sequence and written in this format: Context, Decision, Consequences.

- An ADR is written in the phase in which its decision lands. PLAN.md §16 B lists the ones we expect.
- Once accepted, an ADR is not rewritten. A new ADR supersedes it, and both link to each other.
- Numbers in an ADR come from real runs, cited with the command that produced them.

## Consequences

- Reviewers can see why the system looks the way it does, and what the alternatives were.
- Writing an ADR costs a few minutes per decision, paid at the moment the reasoning is fresh.
- PLAN.md stays the roadmap; the ADRs and the code become the record of what was built.
