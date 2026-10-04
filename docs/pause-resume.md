# Pause / resume

## State model

`PAUSED` is a task status (`TaskStatus.PAUSED`), persisted like any other. It is entered from any
active phase (`PLANNING`, `EXECUTING`, `WAITING_APPROVAL`, `RECOVERING`, `VERIFYING`) and returns to
the phase it interrupted; it can also end in `CANCELLED`, `FAILED` or `TIMED_OUT`.

There is no separate `PAUSING`/`RESUMING` status. Between the request and the moment the task
parks, the status stays what it truthfully is (for example `EXECUTING`, because a tool call is
still in flight) and `task.paused` is `true`.

## Sequence

```
POST /tasks/{id}/pause
  pause_requested                    gate closed; task.paused = true (persisted)
  ... an in-flight tool or model call finishes; nothing new starts ...
  status_changed  EXECUTING -> PAUSED   (persisted)
  pause_completed

POST /tasks/{id}/resume
  resume_requested
  status_changed  PAUSED -> EXECUTING   (persisted, task.paused = false)
  resume_completed                    gate released
```

The order is fixed by construction:

* `TaskRuntime.pause` closes the `PauseGate` and publishes `pause_requested`.
* The task reaches a gate (`RunState.gate()` or the router's two checks). `PauseGate.wait_if_paused`
  calls `RunState.enter_pause`, which sets and saves `PAUSED`, emits `status_changed`, then
  `pause_completed`, and only then waits.
* `TaskRuntime.resume` publishes `resume_requested`, calls `RunState.leave_pause` (restores and
  saves the previous status, emits `status_changed`), then opens the gate, then publishes
  `resume_completed`.

Because the status is restored before the gate opens, the event stream never shows a running
status while the runtime still holds the task, and the persisted status always equals the last
`status_changed` event. Both calls are idempotent. The pause request, the parking and the resume
are serialised by one lock on the task handle (`TaskHandle.pause_lock`), so a slow database write
cannot reorder their events or let a stale write replace the persisted `PAUSED` status.

## What cannot happen while paused

Gates are placed before every operation that costs or acts:

| Operation | Gate |
|---|---|
| first plan (model call) | `runner._lifecycle` |
| next plan step | `runner._execute_plan` |
| every attempt, re-check and recovery of a step | `agent/executor.py` |
| tool call (twice: on entry and right before execution) | `tools/router.py` |
| adaptive iteration (model call) | `agent/adaptive.py` |
| re-plan (model call) | `runner._replan` |
| final verification round, LLM judgement | `runner._lifecycle`, `runner._judge` |

Recovery runs inside the step loop and goes through the same tool and model gates, so no recovery
action, retry or re-plan starts while paused.

An operation already in flight when the pause is requested is not interrupted (a half-finished
tool call is worse than a finished one); the task parks at the next gate. STOP and cancel work on
a paused task: they open the gate so the task observes the cancellation and ends `CANCELLED`.

## Frontend

`taskView.ts` sets `paused` on `pause_requested` and clears it on `resume_completed`; the status
badge shows `PAUSED` from `status_changed`. While a pause is requested but the task has not parked
yet, the UI shows "pausing…". The Resume button is offered whenever `paused` is set or the status
is `PAUSED`. The legacy `task_paused` / `task_resumed` events are still understood for old
timelines but are no longer emitted.

## Metrics

`usage.pause_count`, `usage.resume_count` (also in `TaskResult.metrics`).

## Limits

* Time spent paused counts towards `limits.max_execution_seconds`.
* A pause does not survive a process restart: on startup, non-terminal tasks (including `PAUSED`)
  are marked `FAILED` by `recover_orphans`, as before.

## Tests

`backend/tests/unit/test_runtime_hardening.py` (PAUSED persisted, nothing runs while paused,
event/state order, pause before planning spends no model call),
`tests/e2e/test_scenarios.py::test_pause_and_resume_hold_the_next_tool_call`,
`frontend/tests/taskView.test.ts`.
