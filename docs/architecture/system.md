# System architecture

See [ARCHITECTURE.md](../../ARCHITECTURE.md) for the overview. This page adds the runtime topology.

## Process topology

```
┌──────────────── one Python process (uvicorn) ────────────────────────────────────────────────────┐
│ FastAPI app ── middleware: request-id/log context, TrustedHost, CORS, security headers            │
│   routers /api/v1: tasks, approvals, permissions, agent, system(+tools,settings,metrics) · /ws     │
│   SPA static files (frontend/dist) · /metrics                                                      │
│                                                                                                    │
│ Container (composition root)                                                                       │
│   Repository (Sql | Memory) · EventBus · ToolRegistry · PermissionEngine · ApprovalManager ·        │
│   ToolRouter · LLMGateway(+providers) · Planner · TaskRuntime/TaskRunner · Metrics · TaskLogSink    │
│   ToolServices: PathPolicy · BrowserManager(Playwright) · DesktopService(UIA thread) · VisionService │
└───────────────────────────────┬───────────────────────────────────────────────────┬────────────────┘
                                │ asyncpg                                           │ child processes
                          PostgreSQL                         Chrome/Edge (Playwright) · allowlisted CLI programs
```

## Concurrency model

* `asyncio` throughout; one task = one `asyncio.Task` guarded by a semaphore (`max_concurrent_tasks`).
* Desktop access runs on **one dedicated COM worker thread** (pywinauto/comtypes keep apartment state per thread) and is serialised by a lock
  (there is one mouse and one keyboard).
* Browser: one Playwright browser process, one isolated context per task; contexts are closed when the task ends.
* Blocking work (alembic migrations, UIA calls, subprocess I/O) never runs on the event loop thread.

## Startup / shutdown

`Container.start`: optional `alembic upgrade head` (`AUTO_MIGRATE`) → load persisted policy → start the per-task log sink → classify tasks the
previous process left unfinished as `INTERRUPTED` (nothing is resumed; see [restart-recovery](../restart-recovery.md)).
`Container.stop`: suspend running tasks (10 s grace; they stay resumable, they are not cancelled) → close browsers → stop the UIA thread → flush logs → close LLM clients and the DB engine.
Prod (`SAA_ENV=prod`) refuses to start without an API token.

## Health

`GET /api/v1/health` (liveness, unauthenticated) · `GET /api/v1/ready` (DB ping, LLM configured, browser/desktop/vision availability, kill-switch; 503 if the DB is down).
Prometheus metrics at `/metrics` (authenticated); JSON summary at `/api/v1/metrics/summary`.

## Observability

Structured JSON logs with `request_id, session_id, task_id, plan_id, step_id, tool_call_id` (contextvars), secrets redacted. Metrics (Prometheus, dedicated registry):
`saa_tasks_total{status}`, `saa_task_duration_seconds`, `saa_llm_calls_total{purpose}`, `saa_llm_latency_seconds`, `saa_llm_tokens_total{direction}`, `saa_vision_calls_total`,
`saa_tool_calls_total{tool,capability,outcome}`, `saa_tool_latency_seconds{capability}`, `saa_approvals_total{decision}`, `saa_recoveries_total{strategy}`,
`saa_browser_failures_total`, `saa_desktop_failures_total`, `saa_verification_failures_total`, `saa_policy_denials_total{reason}`,
`saa_injection_findings_total{severity}`, `saa_estimated_cost_usd_total`. Task success/failure rate and average latency are derived in `/metrics/summary`.
