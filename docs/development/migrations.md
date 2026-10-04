# Database migrations

Alembic, async engine, URL from application settings (`SAA_DATABASE__URL`; override with `SAA_MIGRATION_URL`).

```powershell
cd backend
python -m app migrate                                   # alembic upgrade head using the configured URL
.\.venv\Scripts\alembic.exe revision --autogenerate -m "add foo" --rev-id 0002   # after editing app/database/models.py
.\.venv\Scripts\alembic.exe downgrade -1
```

## Workflow

1. Edit `app/database/models.py`.
2. Generate against a scratch SQLite DB: `$env:SAA_MIGRATION_URL="sqlite+aiosqlite:///$env:TEMP\gen.db"; alembic revision --autogenerate -m "…" --rev-id 000N`.
3. **Review and clean the file**: replace `Text()` inside `JSONB(astext_type=Text())` with `JSONB()`, use `postgresql.JSONB` variants as in `0001`, make sure both `upgrade()` and `downgrade()` are complete.
4. Run `pytest tests/integration/test_migrations.py tests/integration/test_repository_contract.py` — they apply the migration to SQLite and a real PostgreSQL, assert **no drift** between migration head and ORM
   metadata, and that downgrade works.
5. Update `docs/architecture/state-model.md` if tables changed.

## Operations

* `SAA_DATABASE__AUTO_MIGRATE=true` runs `upgrade head` at startup (fine for single-user installs); otherwise run `python -m app migrate` as a deploy step.
* Back up with `pg_dump`. The audit tables (`tool_calls`, `permissions`, `approvals`, `task_events`) grow with use; there is no retention job yet.
