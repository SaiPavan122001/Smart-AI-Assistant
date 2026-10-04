# Emergency stop and resource limits

## STOP AGENT

Always visible in the UI header (it pulses while tasks run). **One click, no confirmation** → `POST /api/v1/agent/stop`:

1. `KillSwitch.engage()` — a process-wide flag. Every `CancelToken` observes it, so *every* running task is cancelled.
2. Every task's token is cancelled; pause gates are opened so paused tasks notice; every pending approval is cancelled (counts as denied).
3. The router checks the kill switch/cancel (a) before doing anything, (b) again **after an approval is granted and before the tool starts**,
   so a dangerous operation that was approved a moment ago cannot slip through (`test_cancel_between_approval_and_execution_prevents_the_action`).
4. A running tool is aborted: `CancelToken.run()` cancels the awaiting task; the terminal runner kills the whole process tree
   (`taskkill /T /F`); browser/desktop calls are cancelled at their next await.
5. The task ends `CANCELLED`; its summary says *"Completed actions were NOT undone"* — the agent never pretends it rolled anything back.
6. While engaged, `POST /tasks` returns `409 kill_switch_active` and no tool can execute. **Release stop** (`POST /agent/release`) re-arms it.

PyAutoGUI's fail-safe stays enabled for coordinate input: slamming the mouse into a screen corner aborts automation at the OS-input level.

## Cancel, pause, resume

* *Cancel task*: same mechanism, one task only.
* *Pause*: the next step/tool call (and adaptive iteration) waits at a gate; an in-flight tool call finishes. *Resume* continues.
  Cancel works while paused.

## Limits (per task; `SAA_LIMITS__*`)

| Limit | Default | Enforced in |
|---|---|---|
| Wall-clock time | 1800 s → `TIMED_OUT` | `TaskRunner` (`asyncio.timeout`) |
| Actions (mutating tool executions) | 200 | router |
| LLM calls / vision calls | 60 / 10 | gateway (`UsageMeter`) |
| Shell operations | 40 | router |
| Recovery attempts per step | 3 | `RecoveryEngine` |
| Re-plans | 2 | `RecoveryEngine` / runner |
| Adaptive iterations per step | 12 | `AdaptiveRunner` |
| Browser tabs | 8 | browser session |
| Concurrent tasks | 2 | runtime semaphore |
| Approval wait | 600 s | `ApprovalManager` |

Reaching a limit emits `limit_reached` and ends the task `FAILED` with code `limit_exceeded` (or `TIMED_OUT`); it is never silently ignored.

Tests: `integration/test_api.py::test_emergency_stop_cancels_running_task_and_blocks_new_ones`, `e2e/test_scenarios.py::test_stop_agent_while_a_command_is_running`,
`unit/test_infrastructure.py` (cancel token, pause gate), frontend E2E "STOP AGENT halts a running task immediately".
