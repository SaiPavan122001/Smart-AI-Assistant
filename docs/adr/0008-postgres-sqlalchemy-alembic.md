# ADR-0008: PostgreSQL + SQLAlchemy async + Alembic behind a repository port

**Context.** Required: PostgreSQL, migrations, auditable normalised tables, test speed, no hidden DB coupling in the runtime.

**Decision.** SQLAlchemy 2 (async, asyncpg) models with portable `JSON`/`JSONB` variants; hand-reviewed Alembic migration; `Repository` `Protocol` with memory, SQLite and PostgreSQL implementations proven equivalent by
one contract suite; migrations validated against ORM metadata on SQLite and PostgreSQL (embedded `pgserver` 16.2 in tests; `postgres:16` in compose). `memory://` is an explicit, loudly-logged ephemeral mode.

**Consequences.** + Fast tests and real-DB confidence; schema drift is a test failure. − `merge`-style upserts are simple rather than maximally efficient; large-scale retention/partitioning is future work.
