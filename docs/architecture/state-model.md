# State model and persistence

## Task states

```
CREATED → PLANNING → EXECUTING ⇄ WAITING_APPROVAL
                         │  ⇅ RECOVERING / VERIFYING / PLANNING (re-plan)
                         ▼
                 COMPLETED | FAILED | CANCELLED | TIMED_OUT      (terminal, final)

any unfinished status ──(process stops)──▶ INTERRUPTED ──(user: resume)──▶ RECOVERING ──▶ EXECUTING …
                                               └────────(user: cancel / restart)──▶ CANCELLED
```

`PAUSED` is entered from any active phase and returns to it ([pause-resume](../pause-resume.md)). `INTERRUPTED` is entered only by
startup recovery and left only by an explicit user choice ([restart-recovery](../restart-recovery.md)).

`state/machine.py` is the single authority; `RunState.set_status` validates every transition and emits `status_changed`. After an approval the
phase that was active before `WAITING_APPROVAL` (executing/recovering/verifying/planning) is restored.

## Tables (PostgreSQL; Alembic revisions `0001`, `0002`)

| Table | Purpose |
|---|---|
| `users`, `sessions` | single local user + one session per UI tab (`X-Session-Id`) |
| `tasks` | status, instruction, usage JSON, result JSON, current step/tool/app, timestamps, `request_id`; since `0002`: `durable` (step outputs, recovery history, refusals), `interruption`, `cancel_requested`, `idempotency_key`, `restarted_from` |
| `plans` | one per task (`version` increments on re-plan), summary, assumptions, success criteria; since `0002`: `goal` (the user's explicit goal), `answer_template` |
| `task_steps` | one row per plan step (kind, tool, args, goal, deps, verify, status, attempts, error, output, times) |
| `agent_actions` | model decisions (plan, replan) |
| `tool_calls` | each call: redacted args, rationale, assessment, decision + reason, ok/error code, duration, result. A side-effecting call is written **before** it runs (`decision='executing'`, no result) and updated afterwards; `fingerprint` (since `0002`) identifies it after a restart |
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
head matches the ORM metadata exactly on SQLite *and* PostgreSQL (autogenerate drift = ∅), the migration is reversible, and `0001 → 0002`
keeps existing rows. `save_task` writes the task row, plan, steps and durable state in one transaction.

## Restart behaviour

On startup every unfinished task becomes `INTERRUPTED` (or `CANCELLED`, if the user had asked to stop it). Nothing resumes by itself: the
user resumes, restarts or cancels. A resumed task keeps its completed steps, replays actions that already happened and asks before
repeating one whose outcome is unknown. Full description, guarantees and limits: [restart-recovery](../restart-recovery.md).
History, plans, approvals, policy and logs persist (`tests/integration/test_startup.py`).

## Correlation

`request_id` (HTTP request) → `session_id` (UI tab) → `task_id` → `plan_id` → `step_id` → `tool_call_id`, carried on every event, log line and record.
