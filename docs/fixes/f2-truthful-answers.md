# F2 — truthful final answers

## What was wrong

A task that FAILED could still return a confident success sentence. Seen live in Step 7:
"The file old-backup.zip has been deleted." (the delete was denied and the file still existed) and
"The 'Confirm' button was clicked as requested." (the task failed).

Root cause, in `orchestration/runner.py`:

* `_compose_answer` filled `rs.answer` from the plan's `answer_template` at the start of every
  verification round, before the outcome was known and without looking at step states.
* An adaptive step's `finish_step.answer` was stored in the same task-wide `rs.answer` and never
  cleared when the task later failed.
* `_finalize` copied `rs.answer` into the result for every status.

So "there is some text" was treated as "the task has an answer".

## What it does now

The answer is decided **once, at the end, from final state**, in `app/agent/answer.py`. No model is
involved and no intermediate text is kept.

### Three separate states

`TaskResult` (`app/models/task.py`) carries them explicitly:

| Field | Values | Meaning |
|---|---|---|
| `status` / `outcome` | `COMPLETED`, `FAILED`, `CANCELLED`, `TIMED_OUT` / + `DENIED`, `LOOP_DETECTED`, `PLAN_INVALID` | what happened to the task |
| `verification_status` | `VERIFIED`, `JUDGED`, `INCOMPLETE`, `FAILED`, `NOT_RUN` | what the final contract said |
| `answer_status` | `ANSWERED`, `NOT_REQUESTED`, `answer_unavailable`, `WITHHELD` | how to read `answer` |

`verified` is `true` exactly when `verification_status == VERIFIED`.

### Eligibility for a success answer

`decide_answer` renders a success answer only when all of these hold:

1. `status == COMPLETED`
2. the final verification passed
3. every required step (non-optional, not superseded by a re-plan) is `SUCCEEDED`
4. every `{{...}}` field of the template resolves from the recorded output of a step that
   `SUCCEEDED`, to a non-empty scalar value

Otherwise:

* Any non-COMPLETED outcome: `answer_status = WITHHELD` and `answer` is a fixed, status-specific
  statement from `failure_statement()` — for example
  "The task was not completed: a required action was not approved, so it was not performed."
  or "The task could not be completed. The requested result was not verified. (…)".
  The template is never rendered.
* COMPLETED but a field is missing / null / empty / a list: `answer_status = answer_unavailable`,
  `answer = "The task completed, but the requested response data was unavailable."`,
  `verification_status = INCOMPLETE`, `verified = false`.

### Stale answers

* `RunState.answer` no longer exists. Adaptive answers are kept per step in `step_answers` and
  are removed when the step fails. Only the last `SUCCEEDED`, non-superseded step's answer can be
  used, and only if the plan has no template.
* A re-plan that supplies a new `answer_template` replaces the old one; the final render uses the
  final template against final step outputs.
* The LLM judge (tasks without machine-checkable criteria) is shown a *preview*
  (`candidate_answer`), which commits nothing.

### Invariants

`TaskResult` validates itself; these combinations raise instead of being stored:
`verified=true` on a non-COMPLETED task, `verified=true` with `answer_unavailable`,
`verified=true` without `VERIFIED`, `ANSWERED` on a non-COMPLETED task or with empty text, a
non-COMPLETED task without `WITHHELD`. Results stored before this change are normalised on load
(a failed task's old answer is dropped, never upgraded).

### Two related fixes found by the live re-run

The first live re-run of the new F2 scenario (rename onto an existing file, overwrite denied)
ended COMPLETED/verified although the rename never happened. Two causes, both fixed:

* **A re-plan could replace the success criteria.** `merge_criteria` (`planner/planner.py`) now
  keeps every criterion that is still checkable; a re-plan can only add criteria.
* **Adaptive recovery kept asking after a denial.** When a deterministic step is being recovered
  adaptively and the user denies an action, the loop ends with `approval_denied`
  (`agent/adaptive.py`, `stop_on_refusal`). A planned adaptive step is unchanged: it may continue
  without the denied action.

## Frontend

`ResultCard` shows "Answer:" only for `ANSWERED` on a COMPLETED task. `WITHHELD` text is shown as
"Not completed: …" in an error banner; `answer_unavailable` as "No answer: …". A record without
`answer_status` shows an answer only if it COMPLETED.

## Tests

`backend/tests/unit/test_truthful_answers.py` (failed / cancelled / denied / loop-detected /
plan-invalid / timed-out, missing field, stale answer across re-plans, invariants, legacy records,
criteria cannot be relaxed, denial ends recovery), `tests/unit/test_answer_template.py`,
`frontend/tests/components.test.tsx`.
