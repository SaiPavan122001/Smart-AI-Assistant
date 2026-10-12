# Restart-safe task execution (NEXT-B)

What happens to a task when the backend or the computer stops before the task is finished, and how it
continues afterwards. Code: `app/orchestration/recovery.py`, `app/orchestration/runtime.py`
(`recover_interrupted`, `resume_interrupted`, `restart_interrupted`), `app/orchestration/runner.py`
(`_restore`, `_resume_lifecycle`, `_suspend`), `app/tools/router.py` (write-ahead record, replay,
escalation), `app/tools/resume.py`, `app/models/recovery.py`. Migration: `0002_restart_safe_tasks`.

## What is and is not guaranteed

* A task that was unfinished when the process stopped is **never** reported as failed, cancelled or
  completed by the restart. It becomes `INTERRUPTED` and waits for the user.
* **Nothing resumes by itself.** The user chooses: resume, restart as a new task, or cancel.
* A step that completed is not executed again. An action that completed inside the interrupted step is
  replayed from its recorded result, not executed again.
* An action whose outcome cannot be determined is never repeated without the user's approval.
* There is **no exactly-once guarantee for arbitrary side effects.** A process can die after an effect
  happened and before its result was written. The write-ahead record makes that window *visible*; the
  tool's `reconcile` hook, or the user, resolves it. It cannot be made impossible.

## What is persisted

Everything below is written by the running task, not at shutdown, so it is there after a hard kill.

| State | Where | Written |
|---|---|---|
| Request, status, usage, pause flag | `tasks` | every status change and step boundary |
| The user's explicit goal (requirements, prohibitions), answer template | `plans.goal`, `plans.answer_template` | with the plan |
| Plan, steps, step status / attempts / error | `plans`, `task_steps` | same transaction as the task row |
| Step outputs later steps refer to, adaptive answers, recovery history, re-plan count, refused actions | `tasks.durable` (`DurableState`) | same transaction as the task row |
| A side-effecting action has **started** | `tool_calls` row with `decision='executing'`, no result | before the tool runs |
| The action's result | the same `tool_calls` row | after the tool returns |
| Hash of the tool and its original arguments | `tool_calls.fingerprint` | with the row (stored arguments are redacted, so they cannot identify the call) |
| The user asked to cancel / pressed STOP | `tasks.cancel_requested` | **before** the cancellation takes effect |
| The client's `Idempotency-Key` | `tasks.idempotency_key` | at submit |
| Approvals, permission decisions, events, verification results | their own tables | as before |

`RunState.save()` writes the task row, the plan, every step and the durable state in **one
transaction** (`SqlRepository.save_task`). A failed write changes nothing
(`test_a_failed_task_save_changes_nothing`).

If the write-ahead record cannot be stored, the action is **not performed**
(`test_a_side_effect_does_not_start_when_its_start_cannot_be_recorded`): an unrecorded side effect
could not be recovered.

Not persisted, by design: task-scoped approval grants, the observation cache, the browser session,
the loop guard's ledger, desktop element references. See [What does not survive](#what-does-not-survive).

## States

```
any unfinished status ──(process stops)──▶ INTERRUPTED            entered only by startup recovery
INTERRUPTED ──resume──▶ RECOVERING ──▶ EXECUTING ──▶ … the normal lifecycle
INTERRUPTED ──resume, no plan yet──▶ PLANNING ──▶ …
INTERRUPTED ──cancel / restart──▶ CANCELLED
```

| Required state | Status used |
|---|---|
| queued | `CREATED` |
| running | `PLANNING`, `EXECUTING`, `VERIFYING` |
| paused | `PAUSED` |
| waiting for approval | `WAITING_APPROVAL` |
| interrupted | `INTERRUPTED` (new) |
| recovering | `RECOVERING` (also the first status of a resumed task) |
| completed / failed / cancelled / timed out | terminal, unchanged |
| partial | outcome `PARTIAL` of a `FAILED` task, unchanged (N6) |

`state/machine.py` stays the single authority. `INTERRUPTED` is non-terminal and has no transition to
`COMPLETED`.

## Startup: classify, do not act

`Container.start` → `TaskRuntime.recover_interrupted()`:

1. Every non-terminal task of the previous process is loaded.
2. Its pending approvals are marked `EXPIRED` ("the service restarted before a decision"). An approval
   belongs to the process that asked; it cannot be answered or reused afterwards.
3. `cancel_requested` ⇒ the task is finished as `CANCELLED`. A cancel or STOP the user gave is honoured
   even if the process died before it took effect.
4. Otherwise the task becomes `INTERRUPTED` with an `Interruption` record (`Task.interruption`), a
   `task_interrupted` event and an `interruption` agent action. Announced once, also after several restarts.

The assessment (`assess_interruption`) is read-only. For every action that had started and has no result:

| Certainty | When | On resume |
|---|---|---|
| `safe_to_retry` | the tool is declared `idempotent` (`fs_create`, `fs_organize`) | runs again through the normal checks |
| `confirmed_done` | `Tool.reconcile` found the effect in the observable state | its result is **replayed**; it is not executed |
| `confirmed_not_done` | `reconcile` found it did not happen | runs again through the normal checks |
| `uncertain` | everything else | needs the user's approval, for that one call |

`reconcile` exists for `fs_move`, `fs_copy`, `fs_delete` (answered from the files only; a copy counts as
done only when the destination is byte-identical to the source) and `browser_open` / `browser_navigate`
(the isolated session is gone, so loading a page again repeats nothing). Clicks, typing, uploads,
downloads, desktop input, application launch and terminal commands have no reliable observable answer
and are `uncertain`. So is any action whose stored arguments were masked.

## Resume

`POST /tasks/{id}/recover {"action": "resume"}` → `TaskRuntime.resume_interrupted` → `TaskRunner.run(resume=True)`:

1. The run state is rebuilt from the plan, the steps and `DurableState`. The goal is the persisted one;
   it is not extracted again and cannot change.
2. In-flight actions are assessed **again** (files may have changed while the task waited).
3. What the task had seen and written (`Observed`) and its prompt-injection taint are rebuilt from the
   recorded tool results, so goal evaluation and injection protection behave as before the stop.
4. Steps that were `RUNNING` or `WAITING_APPROVAL` go back to `PENDING` and start again. The router
   holds a `ResumeLedger`:
   * a side-effecting call whose fingerprint already **completed** in an unfinished step is answered
     from its recorded result (`decision='replayed'`), once per recorded occurrence;
   * a call whose fingerprint is **uncertain** is forced to `ASK` by the permission engine
     (`via='restart_uncertain'`), once-only scope, whatever the policy, a trusted operation or a task
     grant would allow. The approval explains why. Once it has run again, the old record is closed.
5. If the task had used the browser and remaining steps need an open page, the plan is revised first
   (one model call) with the reason that the session is gone. Remaining steps that only open pages, or
   do not use the browser, need no model call.

A resumed task is an ordinary task: same router, permission engine, approvals, goal enforcement,
limits, loop guard, pause and STOP. Usage counters continue, so budgets are not reset.

## What does not survive

| Lost | Consequence |
|---|---|
| Task-scoped approval grants | the user is asked again after a restart (deliberate) |
| A pending or just-granted approval | void; the action is asked again with its exact arguments |
| Browser session, open pages, element refs | pages are opened again; a revised plan if needed |
| Desktop element refs; windows may have changed | looked up again; normal stale-reference recovery |
| Loop guard ledger, observation cache | start empty |
| The execution-time limit | counts from the resume |
| The emergency stop itself | released by a restart; harmless because nothing resumes by itself |

A refusal survives: a call the user denied stays denied (`DurableState.denied`).

## The user's options

| Option | API | Effect |
|---|---|---|
| Inspect | `GET /tasks/{id}/recovery` | the interruption (re-assessed) and a report of steps done / interrupted / not run |
| Resume | `POST /tasks/{id}/recover` `resume` | as above; refused while STOP is engaged |
| Restart | `recover` `restart` | the task ends `CANCELLED`; the same request runs as a new task (`restarted_from`). The new task does not know what the old one did |
| Cancel | `recover` `cancel` or `POST /tasks/{id}/cancel` | `CANCELLED` with a truthful report; nothing is undone. Its file changes can then be rolled back on request ([file-transactions](file-transactions.md)) |

Sending the same request again while its task is interrupted returns the interrupted task instead of
starting a second run. An `Idempotency-Key` is honoured across restarts.

## Service shutdown

A normal stop (`TaskRuntime.shutdown`) *suspends* running tasks (`CancelToken.suspend`): they are not
finalized as cancelled, and an action that was running keeps its in-flight record. The next start treats
them exactly like tasks of a killed process.

## Events

`seq` continues after a restart (`EventBus` seeds from storage) and `(task_id, seq)` is unique in the
database, so a replay never duplicates or reorders. New events: `task_interrupted`, `task_resuming`.
The dashboard reducer is idempotent on `seq`.

## Tests

| Requirement | Test |
|---|---|
| Restart during a running task | `test_a_task_running_at_a_restart_is_interrupted_not_failed` |
| Just before / just after a side effect | `test_restart_just_before_a_side_effect_runs_it_exactly_once`, `test_restart_just_after_a_side_effect_does_not_repeat_it`, `test_result_recorded_but_step_not_completed_is_replayed` |
| While awaiting approval | `test_restart_while_awaiting_approval_asks_again_and_never_acts_by_itself`, `test_an_approval_given_before_the_crash_is_not_reused`, `test_a_task_scoped_grant_does_not_survive_a_restart`, `test_a_denial_after_a_restart_is_final`, `test_a_refusal_from_before_the_restart_is_still_final` |
| Pause, resume, cancel, STOP | `test_restart_while_paused_resumes_only_when_the_user_asks`, `test_a_cancel_recorded_before_the_crash_is_honoured`, `test_stop_recorded_before_the_crash_is_honoured`, `test_resume_is_refused_while_the_emergency_stop_is_engaged`, `test_stop_during_a_resumed_task_cancels_it` |
| Database transaction failure | `test_a_side_effect_does_not_start_when_its_start_cannot_be_recorded`, `test_a_failing_task_save_ends_the_task_without_a_success_claim`, `test_a_failed_task_save_changes_nothing` |
| Duplicate submission, repeated events | `test_the_same_request_is_not_started_next_to_an_interrupted_task`, `test_an_idempotency_key_is_honoured_across_a_restart`, `test_resuming_twice_runs_the_task_once`, `test_events_stay_ordered_and_unique_across_a_restart`, `test_a_duplicate_event_sequence_number_is_rejected` |
| Duplicate file operations | `test_multi_file_organisation_is_not_repeated_after_a_restart`, `test_file_actions_are_reconciled_from_the_files_only` |
| Completed steps not re-executed | `test_completed_step_outputs_survive_and_feed_later_steps` and the execution counts in the tests above |
| Uncertain browser / desktop effect | `test_classification_of_an_action_that_was_in_flight`, `test_an_uncertain_action_is_never_repeated_without_approval`, `test_an_uncertain_action_runs_again_only_after_the_user_approves`, `test_uncertainty_survives_a_second_restart`, `test_a_lost_browser_session_leads_to_a_revised_plan_not_to_blind_clicks` |
| Goal, limits, permissions after a resume | `test_a_resumed_task_keeps_its_usage_and_its_explicit_goal`, `test_a_trusted_operation_or_grant_cannot_wave_an_uncertain_action_through` |
| Real storage, real process death | `tests/integration/test_restart_durable.py` (memory, SQLite, PostgreSQL; a process killed with `TerminateProcess`) |
| Migration | `tests/integration/test_migrations.py` (drift on SQLite and PostgreSQL, 0001 → 0002 with existing rows, downgrade) |
| API, dashboard | `tests/integration/test_api_recovery.py`, `frontend/tests/recovery.test.tsx` |

The simulated restarts (`tests/restart_helpers.py`) are two independent runtimes over one storage; the
first stops receiving writes at an exact point. Mutation check: switching off the replay, the escalation
of uncertain actions, the write-ahead record, the restored refusals, the `INTERRUPTED` classification or
the persisted cancel request each fails at least one test.

## Validation record (2026-10-10)

Windows 11 (10.0.22631), Python 3.12, Node 22, 8 GB RAM. Commands run from `backend/` with the project virtualenv, or from `frontend/`.

The all-in-one `scripts\check.ps1` was started once and **did not finish**: it was stopped at 17 % of the backend suite because the
machine ran critically low on memory. That was not a test failure (144 tests had passed, none had failed). It was not restarted.
The same component commands were then run **one group at a time**; each exited 0:

| # | Group | Command | Result |
|---|---|---|---|
| 1 | Format, lint, types | `ruff format --check app tests evals` · `ruff check app tests evals` · `mypy` | format clean for 211 files (see note) · lint clean · 0 issues in 146 files |
| 2 | Restart recovery | `pytest tests/e2e/test_restart_recovery.py` | 44 passed |
| 3 | Storage and migrations | `pytest tests/integration/test_restart_durable.py tests/integration/test_migrations.py tests/integration/test_repository_contract.py` | 33 passed, 2 skipped |
| 4 | API | `pytest tests/integration/test_api_recovery.py tests/integration/test_api.py` | 21 passed (5 recovery, 16 existing) |
| 5 | Unit | `pytest tests/unit` | 529 passed, 1 skipped |
| 5 | Security | `pytest tests/security --ignore=…/test_path_confinement.py --ignore=…/test_permission_hardening.py` | 113 passed |
| 5 | End to end (scripted model) | `pytest tests/e2e` | 107 passed (includes the 44 of group 2) |
| 5 | UI Automation, real server, real browser | `pytest tests/integration/test_uia_windows.py`, `…/test_startup.py`, `…/test_browser.py` | 3 passed, 1 skipped · 5 passed · 18 passed |
| 6 | Evaluation suite | `python -m evals.run` | 17/17, 46 model calls (unchanged), 0 security violations, 0 permission bypasses, thresholds met |
| 7 | Frontend | `npm run lint` · `npm run typecheck` · `npm test` | clean · clean · 54 passed (45 existing, 9 new) |
| 8 | Production build | `npm run build` | OK |
| 9 | Playwright UI E2E | `npm run test:e2e` | 8 passed |

Backend total: **829 passed, 4 skipped, 0 failed** over every test file except the two named below (NEXT-A: 762 passed, 2 skipped).
Skips: symlink privilege (as at v0.1.0); the opt-in visible-Notepad test (as at v0.1.0); two SQL-only tests on the in-memory variant.

Not included, and why:

* `tests/security/test_path_confinement.py` and `tests/security/test_permission_hardening.py` are **drafts for NEXT-C**. They assert
  fixes that do not exist yet (junction-blind directory traversal, approvals re-checked after waiting) and are expected to fail until
  NEXT-C is implemented. They were not run, are not part of the NEXT-B commit, and are the only two files `ruff format --check` flags.
  No NEXT-C application code has been written.
* No single uninterrupted run of the whole check exists; the table above is the evidence. The groups cover the same commands.
* Live model validation did not run. `python -m app llm-check` returned `403 Key limit exceeded (total limit)` for every probe; the
  provider, model and credentials were not changed. NA1–NA5, the 18-scenario gate and any live interruption test remain open.

Two existing tests failed once during development and pass now, with the tests unchanged:
`test_pause_before_planning_spends_no_model_call` (its test double has no `resume` parameter; the runtime now passes the argument
only when resuming) and `test_every_active_state_can_be_cancelled_failed_or_timed_out` (the new `INTERRUPTED` state lacked a
transition to `TIMED_OUT`).

Mutation check, re-run after the groups above: switching off the replay of completed actions, the approval for uncertain actions,
the write-ahead record, the restored refusals, the `INTERRUPTED` classification at startup, or the persisted cancel request each
fails at least one recovery test (6 of 6 caught).

## Limits

1. Reconciliation covers file moves, copies and deletes and browser page loads. Every other in-flight
   side effect is `uncertain` and costs the user one approval after a restart.
2. Refusing the approval of an uncertain action ends the task as `DENIED`, even when the action had in
   fact completed. The interruption report says which action it was; the task can be restarted.
3. An interrupted *adaptive* step starts again. The model is told which of its actions already
   completed and identical calls are replayed, but a model may choose different arguments; such a call
   is a new action and goes through the normal permission checks.
4. A crash between the user's denial and the next task save (a few milliseconds) can lose the denial;
   the action is then asked again, never executed without asking.
5. Tasks stored before migration `0002` have no step outputs: restart or cancel only.
6. Recovery was exercised with deterministic tests and a scripted model. No live-model task has been
   interrupted and resumed (the live gate is blocked, see [next-a/validation-results.md](next-a/validation-results.md)).
7. Not tested: a power loss that corrupts the database, a full disk, two backend processes on one database.
