# Execution-loop fix (P0): repeated OPEN / CLOSE of an application

**Status: root-cause defects fixed and covered by deterministic tests. Real-world computer validation is PENDING.**
Nothing in this document was verified against a real application, a real browser window or a live model.

## 1. Original symptom

During real testing the agent opened an application, it closed, it opened again, and so on until a human stopped it.

## 2. What evidence exists (and what does not)

* **No incident record survives.** The live harness runs on `memory://` (in-memory repository): when the process exits, every task,
  event, tool call and log is gone, and no log file is written anywhere. `~/SmartAIAssistant/data` is empty. No server was running afterwards.
  The only thing on disk from the validation runs is `backend/validation/results/*.json` (per-scenario summaries: tool names, outcomes, recovery
  strategies). None of those records contains a repeated `desktop_launch` or any close.
* Therefore **the exact action sequence of the incident could not be reconstructed**, and this document does not claim to have proven which of the
  defects below fired. What it proves, from the code, is that **each defect on its own allows the observed cycle**, and it fixes all of them.
* Missing observability that made this unprovable: no durable store in the default dev configuration, no per-task action ledger, no event recording
  "this exact side effect already happened".

## 3. Root cause (code-proven)

The runtime had **no memory of which side effects had already happened in a task**, and nothing stopped identical side effects from being
re-issued by *any* of the layers that can issue an action:

| # | Defect | Where | Effect |
|---|--------|-------|--------|
| D1 | `desktop_launch` always started a new process; it never checked for an already-open window | `DesktopService.launch` | every launch = another window |
| D2 | A step whose action **succeeded** but whose verification **failed** was recorded as `FAILED` ("Failed: …"), the replanner was only told about `SUCCEEDED` steps, and the adaptive-fallback goal said "choose a different way … **and perform it**" | `executor._fail`, `planner.replan`, `recovery._materialise` | a verification false-negative (we observed these on real Notepad) led to *re-launch by the adaptive loop* and *re-launch by the replanned step list* |
| D3 | All per-loop counters were local and reset: the adaptive `seen` map per adaptive run, recovery history per step id (a replan creates new step ids), `max_replans` only bounds replans, and the adaptive breaker only *returned an error string* (the loop continued to its 12th iteration) | `adaptive.py`, `recovery/engine.py` | an OPEN/CLOSE pattern spread across steps/replans was never recognised; the only real ceilings were 200 actions / 60 LLM calls / 30 min |
| D4 | `POST /tasks` was not idempotent and `max_concurrent_tasks` defaulted to **2**: a double click, an HTTP retry or two clients created two tasks that drove the same desktop at the same time | `runtime.submit`, `LimitSettings` | two runtimes issuing `desktop_launch` / browser open+close against one computer |
| D5 | The "close" half: **no code in the application closes a desktop window** (there is no close tool; `desktop_key` can send Alt+F4/Ctrl+W, which is approval-gated). A window disappears when (a) the model chose a close action/hotkey, (b) a browser session ends (`TaskRunner._finalize` -> `close_session` closes that task's Chrome window), or (c) the user/OS closes it. With D4, *each duplicate task opens a browser window and closes it at its own end*, which is exactly an OPEN/CLOSE/OPEN/CLOSE rhythm. | `runner._finalize`, `BrowserManager.close_session` | by-design cleanup repeated once per duplicate task |

What was checked and found **not** to be a cause: WebSocket replay (read-only; never executes, `ws.py::_pump`), step state machine (a step can
never leave `SUCCEEDED`/`FAILED` for `PENDING`; `PENDING` is only the creation default and `_execute_plan` only picks `PENDING`),
router retries (`desktop_launch` has `max_attempts=1`), recovery after success (`_attempt` returns before recovery runs), the harness
(one container per run, no cleanup that closes apps), and cancellation (see section 6).

## 4. Exact execution paths that could produce the cycle

```
UI double-click / retry --POST /tasks x2--> Runtime (2 concurrent) --> Task A: browser_open (window opens) ... finalize closes it
                                                                  \-> Task B: browser_open (window opens) ... finalize closes it   (D4+D5)

plan: s1 desktop_launch ok -> verify FAILS (false negative) -> REFRESH_RETRY (re-check only) -> ADAPTIVE ("perform it") -> launch again (D2)
      -> adaptive fails -> REPLAN (planner sees s1 as plain FAILED) -> new plan starts with desktop_launch again -> launch again (D2/D3)
      -> model "cleans up" the extra windows by closing them -> next recovery/replan opens again ...                          (D1+D2+D3)
```

## 5. Why it was allowed to continue

Every guard was local to one loop. Nothing at the choke point every action passes through (`ToolRouter`) knew that "open notepad" had already
been done, or that the last six actions were `open, close, open, close, open, close`. Only global ceilings existed.

## 6. Code changes

| Area | Change |
|------|--------|
| **Action ledger / loop guard** (new `app/tools/loop_guard.py`, owned per task by `TaskHandle`, enforced in `ToolRouter`) | Fingerprints every call (`tool + canonical args`). **Idempotent replay**: an identical state-establishing action (`Tool.establishes_state`: `desktop_launch`, `browser_open`) that already succeeded, with no later side effect on the same capability, is **not executed again**; the router returns the recorded result marked `replayed` ("Already done earlier in this task; NOT repeated"). **Loop detection** (sticky `LOOP_DETECTED`): same side effect N times in a row, an A,B,A,B… cycle repeated N times, N consecutive calls with identical call *and* result (no progress), or >3 asks to repeat an already-done action. |
| **Tripping blocks everything** | After `LOOP_DETECTED` the router refuses every tool call (reads included), the executor will not attempt/recover/retry, the adaptive loop aborts mid-batch, and `_replan` / final-verification rounds raise. The task ends `FAILED` with code `loop_detected`; a `limit_reached` event (`code=loop_detected`, reason, counters) and a durable `loop_detected` agent-action record are written exactly once (`report_loop`, also called from `_finalize`). |
| **Adaptive breaker escalation** | Three ignored "you already made this exact call" refusals now trip the guard instead of silently continuing to iteration 12. |
| **Verification uncertainty never repeats a side effect** | `FAILED` steps whose action ran (reached verification in any attempt) are marked `output.action_performed`. The replanner is told `performed_but_unverified` ("effects are real; never re-open/re-launch; inspect instead"). The adaptive-fallback goal for verification failures now says the action ALREADY RAN and must not be repeated. Recovery for a verify-phase failure still re-checks without re-running (`skip_action`). |
| **State check before launching** (`DesktopService.launch`) | If an app of the same executable (`calc.exe` ~ `CalculatorApp.exe`, `Code` ~ `Code.exe`) already has a window and no arguments were given, it is focused and reused (`reused: true`) – no new process. `new_instance=true` overrides it; `explorer` always opens a new window. |
| **Idempotent task start** (`TaskRuntime.submit`, `POST /tasks`, UI) | `Idempotency-Key` header (bounded 512-entry map) and a duplicate-while-running guard on the normalised instruction; both decided and registered *before the first `await`*, so concurrent duplicates collapse. UI: a ref-based in-flight guard (state was too slow for same-tick double submits) and a fresh key per user action. |
| **One computer, one agent** | `max_concurrent_tasks` default 2 -> **1** (tasks queue). |
| **Cancellation** | Already checked in router entry, before execution, executor loop, adaptive loop, `_execute_plan`; added before `_replan` (a STOP can no longer cause a new LLM call/plan), pause-aware replans and final-verification rounds. |
| **Settings** | `max_identical_actions=6`, `max_cycle_repeats=3`, `max_no_progress=5` (secondary nets; documented in `.env.example`). |

Not changed: permission engine, path/command/URL policies, approvals, injection defences, audit trail, resource limits (all still apply to
every real execution; a replay performs no action and therefore needs no new permission).

## 7. Why the application "closed"

See D5: nothing closes desktop windows by design. Closing comes from a model-chosen close action/hotkey, from per-task browser-session cleanup (once
per task, so once per duplicate task), or from the user. The fix targets the *repetition* that makes it look like a loop.

## 8. Regression tests (all deterministic: fake desktop, scripted LLM, test-only close tool; no real window)

`backend/tests/unit/test_loop_protection.py` (19 tests), `tests/integration/test_api.py` (2), `tests/unit/test_planner.py`, frontend (2):

| Required case | Test |
|---------------|------|
| 1 open succeeds, no second open | `test_open_succeeds_once_and_the_step_completes_without_a_second_open` |
| 2 model keeps requesting the same open -> bounded, LOOP_DETECTED | `test_model_repeatedly_requesting_the_same_open_is_bounded_and_detected` |
| 3 uncertain verification after open | `test_failed_verification_after_open_never_reopens_and_ends_safely` |
| 4 recovery after success / terminal states | executor assertion in test 1 + `test_recovery_never_retries_terminal_or_cancelled_or_loop_states` |
| 5 re-planner re-lists the completed open step | `test_replanner_returning_the_completed_open_step_does_not_execute_it_again` |
| 6 two identical task starts | `test_two_identical_task_starts_run_once`, `test_idempotency_key_…`, API `test_repeated_delivery_…`, UI double-submit + `Idempotency-Key` tests |
| 7 WebSocket reconnect | `test_websocket_reconnects_only_replay_state_and_never_execute` |
| 8 STOP during the loop | `test_stop_during_the_loop_runs_no_further_tool_call` |
| 9 pause during the loop | `test_pause_blocks_new_actions_until_resumed` |
| 10 app already open | `test_app_already_open_is_recognised_and_not_relaunched`, `test_calculator_alias_and_explorer_exception` |
| 17 simulated OPEN/CLOSE/OPEN/CLOSE | `test_simulated_open_close_open_close_cycle_stops_instead_of_continuing` (stops after the 3rd pair: 3 opens, 3 closes, 1 LLM call) |
| ledger unit tests | replay only while nothing changed; failed opens never replayed; identical/cycle/no-progress/sticky; legitimate variety is not a loop; replay requests bounded |

## 9. Test results (non-live only)

* Backend: **561 passed, 2 skipped** (`pytest`, includes unit, integration, eval gate, docs-sync, architecture and the pre-existing e2e suite);
  ruff, ruff format, mypy (124 files) clean.
* Frontend: **36 passed** (Vitest), `tsc` and ESLint clean.
* Disclosure: the pre-existing e2e tests marked `browser` run **headless** Chrome against a local test site as part of the standard suite and were included in
  that run. No live LLM, no `validation.live`, no Notepad/Calculator/desktop automation and no shell scenario was run for this fix.

## 10. Remaining risks

* The incident's true sequence is unproven (section 2). If it was caused by something outside the paths analysed (e.g. a model-issued close hotkey the user
  approved), the ledger will still stop the repetition, but the first iteration's cause would remain unknown. **Make the default dev repository durable
  (SQLite) so the next incident leaves a record.**
* A model that varies arguments each time (e.g. close with a different title every time) can still avoid the *cycle* detector; the per-task ceilings
  (`max_actions`, `max_llm_calls`, `max_execution_seconds`) remain the backstop there.
* Reuse of an open window is a policy choice: a user who really wants a second window must say so (`new_instance`).
* Replay is keyed on identical arguments and on "no later side effect on that capability"; a user closing the window by hand *between* two identical
  launch requests is invisible to the ledger (the window-state check in `launch` covers that case for desktop apps).
* Verification false negatives still happen (seen on real Notepad); they are now harmless for side effects but still cost LLM calls.

## 11. Needs manual verification next (real machine, single user)

1. Notepad: "Open Notepad and type …" – exactly one Notepad window, no relaunch if Notepad was already open.
2. Calculator: "Open Calculator and calculate 25 x 16" – one window; check the new `reused` summary when Calculator is already open.
3. Chrome: "Open Chrome and search …" – one window per task; submit the same request twice quickly and via the Idempotency-Key retry: one task.
4. Force a verification mismatch (e.g. an app whose title never matches) and confirm: no relaunch, bounded recovery, honest failure.
5. STOP and Pause mid-run; confirm no action after STOP.
6. Look for `loop_detected` events only when you deliberately provoke a loop.
