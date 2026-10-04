# API (v1)

Base path `/api/v1`. The OpenAPI document is generated from the code: [`openapi.json`](openapi.json) (also served at `/api/docs` when the server runs). Regenerate with `python -m app.docs_gen`.

**Authentication:** `Authorization: Bearer <SAA_SECURITY__API_TOKEN>` on everything except `/health` and `/ready`. WebSockets: subprotocols `["saa.v1", "bearer.<token>"]`.
Errors share one shape: `{"error": {"code", "message", "request_id", "details"}}`.

| Method & path | Purpose |
|---|---|
| `GET /health` · `GET /ready` | liveness · readiness (DB, LLM, browser, desktop, vision, kill switch; 503 if DB down) |
| `POST /tasks` `{instruction}` | create a task (201). `409 kill_switch_active` while the emergency stop is engaged |
| `GET /tasks?limit&offset&status` | history |
| `GET /tasks/{id}` | task with plan, steps and result |
| `POST /tasks/{id}/cancel` · `/pause` · `/resume` | control (422 if already terminal) |
| `GET /tasks/{id}/events?after_seq&limit` | ordered timeline events |
| `GET /tasks/{id}/logs?level&limit` | redacted execution logs |
| `GET /tasks/{id}/tool-calls` · `/approvals` · `/verifications` · `/permission-decisions` · `/agent-actions` | audit details |
| `GET /approvals?pending=true` · `GET /approvals/{id}` | approval requests |
| `POST /approvals/{id}/approve` `{scope: once|task, note}` · `/deny` `{note}` | decide (422 if already decided) |
| `GET /permissions` · `PUT /permissions` | policy (validated; unknown tools rejected; CRITICAL can't be auto-allowed) |
| `GET /permissions/decisions?task_id=` | decision audit |
| `POST /agent/stop` · `POST /agent/release` · `GET /agent/status` | emergency stop / release / status |
| `GET /tools` | tool specs (schema, capability, risk, approval, timeout, retry, idempotency, audit) |
| `GET /settings` | effective non-secret configuration |
| `GET /metrics/summary` · `GET /metrics` | JSON summary · Prometheus text |
| `WS /ws/tasks/{id}?after_seq=N` · `WS /ws/events` | live events (see [realtime-events](../architecture/realtime-events.md)) |

Example:

```bash
curl -s -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
     -d '{"instruction":"Create a folder called Reports on my desktop"}' http://127.0.0.1:8000/api/v1/tasks
```

Versioning: breaking changes get `/api/v2`; additive changes (new event types/fields) do not bump the version, so clients must ignore unknown fields/event types.
