# Realtime events

## Transport

* `GET ws://…/api/v1/ws/tasks/{task_id}?after_seq=N` — replay persisted events with `seq > N`, then live events.
* `GET ws://…/api/v1/ws/events` — live events for all tasks (no replay).
* Auth: subprotocols `["saa.v1", "bearer.<token>"]` (the token never appears in the URL; `?token=` is not accepted, non-browser clients send the same subprotocol header).
  The server verifies `Origin` against `SAA_SECURITY__ALLOWED_ORIGINS` and closes with `1008` on a bad token/origin (clients must not retry).
* Server → client only; the client may send `ping` and receives `pong`. A subscriber that cannot keep up is closed (`1013`) and reconnects with `after_seq`.
* The subscription is registered *before* the replay query, and live events with `seq <= last replayed` are dropped, so reconnects have no gaps and no duplicates.

## Event schema

```json
{ "id": "evt_…", "task_id": "task_…", "seq": 17, "type": "action_completed", "timestamp": "2026-…Z",
  "correlation": { "request_id": "…", "session_id": "…", "task_id": "…", "plan_id": "…", "step_id": "s3", "tool_call_id": "tc_…" },
  "message": "Created folder: C:\\…", "data": { … } }
```

`seq` is strictly increasing per task (and continues after a restart). `data` is always redacted.

## Event types

| Type | Meaning / notable `data` |
|---|---|
| `task_created` | `task{id, instruction, status}` |
| `status_changed` | `status`, `previous` (state machine transitions) |
| `planning_started`, `plan_created`, `plan_revised` | `plan` (full), `reason` for revisions |
| `step_started`, `step_completed`, `step_failed` | `step_id`, `title`, `attempts`; `skipped`/`superseded` flags |
| `tool_selected` | `tool, capability, args(redacted), risk, decision, reason, resource` |
| `approval_required`, `approval_granted`, `approval_denied` | full `approval` request / `approval_id`, `scope`, `status` |
| `action_started`, `action_completed`, `action_failed` | `tool`, `ok`, `duration_ms`, `perception_source`, `code`, `retryable` |
| `observation_received` | `tool`, `source`, `observation_id`, `untrusted`, `cached` |
| `injection_detected` | `tool`, `findings[{rule,severity,excerpt}]`, `tainted`, or `blocked` for provenance blocks |
| `recovery_started`, `recovery_completed` | `strategy`, `error_code`, `phase`, `attempt`, human `reason` |
| `verification_started`, `verification_completed` | `scope` (`step sN`/`task`), `passed`, `results[]`, `method` |
| `usage_updated` | the `UsageStats` snapshot |
| `pause_requested`, `pause_completed`, `resume_requested`, `resume_completed` | pause lifecycle, always in this order around the `status_changed` to/from `PAUSED` ([pause-resume](../pause-resume.md)); `task_paused`/`task_resumed` are legacy names, no longer emitted |
| `limit_reached`, `kill_switch` | control events |
| `task_completed`, `task_failed`, `task_cancelled`, `task_timed_out` | `result` (`TaskResult`) |

## Reconstructing the timeline

The UI (`frontend/src/features/tasks/taskView.ts`) is a pure, idempotent reducer over events: duplicates/replays change nothing; steps own their
tool/observation/approval/recovery lines; terminal events settle anything still "running". `buildTaskView(events)` produces the same view as the live stream
(tested in `frontend/tests/taskView.test.ts`).

## REST equivalents

`GET /tasks/{id}/events?after_seq=&limit=` returns the same persisted events (used by the details page for finished tasks).
