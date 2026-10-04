# State model and persistence

## Task states

```
CREATED → PLANNING → EXECUTING ⇄ WAITING_APPROVAL
                         │  ⇅ RECOVERING / VERIFYING / PLANNING (re-plan)
                         ▼
                 COMPLETED | FAILED | CANCELLED | TIMED_OUT      (terminal, final)
```

`state/machine.py` is the single authority; `RunState.set_status` validates every transition and emits `status_changed`. After an approval the
phase that was active before `WAITING_APPROVAL` (executing/recovering/verifying/planning) is restored.

## Tables (PostgreSQL; Alembic revision `0001`)

| Table | Purpose |
|---|---|
| `users`, `sessions` | single local user + one session per UI tab (`X-Session-Id`) |
| `tasks` | status, instruction, usage JSON, result JSON, current step/tool/app, timestamps, `request_id` |
| `plans` | one per task (`version` increments on re-plan), summary, assumptions, success criteria |
| `task_steps` | one row per plan step (kind, tool, args, goal, deps, verify, status, attempts, error, output, times) |
| `agent_actions` | model decisions (plan, replan) |
| `tool_calls` | each call: redacted args, rationale, assessment, decision + reason, ok/error code, duration, result |
| `observations` | structured observations (source, kind, subject, content hash, untrusted, approx tokens) |
| `permissions` | the persisted policy (singleton `id='policy'`) and the per-call decision audit |
| `approvals` | full request + status/scope/decider/note/timestamps |
| `execution_logs` | redacted log lines captured per task (level, component, correlation) |
| `verification_results` | each check: kind, passed, evidence, message, method (`deterministic` / `llm_judgement`) |
| `task_events` | ordered timeline events (`task_id, seq` unique) |

JSON columns are `JSONB` on PostgreSQL and `JSON` elsewhere. Timestamps are timezone-aware UTC.

## Repository port

`state/repository.py::Repository` is a `Protocol` with three implementations — `InMemoryRepository`, `SqlRepository` on SQLite, and on PostgreSQL —
all verified by the same contract suite (`tests/integration/test_repository_contract.py`). Migrations are validated by `tests/integration/test_migrations.py`:
head matches the ORM metadata exactly on SQLite *and* PostgreSQL (autogenerate drift = ∅) and the migration is reversible.

## Restart behaviour

Running tasks cannot be resumed after a process restart. On startup, non-terminal tasks are marked `FAILED` with an explicit "interrupted by a service restart;
actions already performed were not undone" event. History, plans, approvals, policy and logs persist (`tests/integration/test_startup.py`).

## Correlation

`request_id` (HTTP request) → `session_id` (UI tab) → `task_id` → `plan_id` → `step_id` → `tool_call_id`, carried on every event, log line and record.
