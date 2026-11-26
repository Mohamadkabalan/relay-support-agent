# Architecture decision records

| ADR | Decision | Status |
|---|---|---|
| [0001](0001-record-architecture-decisions.md) | Record architecture decisions | Accepted |
| [0002](0002-hand-written-loop.md) | A hand-written loop on the Messages API: no agent framework, no SDK tool runner, no API-side MCP connector | Accepted |
| [0003](0003-identity-in-the-transport.md) | Customer identity is bound in the transport, never in tool arguments | Accepted |
| [0004](0004-confirm-then-commit.md) | Confirm-then-commit for write tools | Accepted |
| [0005](0005-append-only-transcript.md) | Append-only transcript with raw provider payloads | Accepted |
| [0006](0006-policy-outcomes-are-data.md) | Policy outcomes are data; failures are errors | Accepted |
| 0007 | Turns run in the API process under a Redis single-flight lock | Planned (Phase 5) |
| 0008 | Eval trials run on cloned template databases with ephemeral MCP servers | Planned (Phase 4) |
| [0009](0009-retrieval-vector-search.md) | Help-center retrieval uses local embeddings (vector search) | Accepted |
| 0010 | Phoenix for tracing | Planned (Phase 5) |
| 0011 | Deployment on one VM with Compose and Caddy | Planned (Phase 8) |

New ADRs copy the format of [0001](0001-record-architecture-decisions.md): Context, Decision, Consequences.
