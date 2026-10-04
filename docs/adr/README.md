# Architecture decision records

| # | Decision | Status |
|---|---|---|
| [0001](0001-modular-monolith.md) | Modular monolith, no extra infrastructure | accepted |
| [0002](0002-perception-hierarchy.md) | Structured perception first; vision is an explicit, metered fallback | accepted |
| [0003](0003-playwright-library-not-mcp.md) | Playwright as a library; no MCP layer | accepted |
| [0004](0004-permission-engine-boundary.md) | Router + permission engine as the only path to the computer | accepted |
| [0005](0005-untrusted-data-and-taint.md) | External content is data; deterministic detection + taint + provenance | accepted |
| [0006](0006-plans-deterministic-steps.md) | Plans of deterministic steps with bounded adaptive steps | accepted |
| [0007](0007-event-sourced-ui.md) | Event-sourced UI over WebSocket with sequence replay | accepted |
| [0008](0008-postgres-sqlalchemy-alembic.md) | PostgreSQL + SQLAlchemy async + Alembic, behind a repository port | accepted |
| [0009](0009-controlled-terminal.md) | No shell: parsed, classified, allowlisted commands | accepted |
| [0010](0010-llm-provider-abstraction.md) | Provider-agnostic LLM interface over plain HTTP | accepted |
| [0011](0011-verification-contracts.md) | Declarative verification contracts; success is observed, not assumed | accepted |

Format: context → decision → consequences (including what we gave up).
