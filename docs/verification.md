# Verification

Verification is deterministic: checkers in `backend/app/verification/checks.py` inspect real
state (files, the browser page, windows, recorded step outputs). An LLM judgement is used only
when a task has no machine-checkable contract at all, and such a result is never `verified`.

## Contracts

A `VerificationSpec` is `{kind, params, description}`. Each kind has a parameter model registered
with its checker; unknown or missing parameters are rejected at plan time and again at run time.

| Kind | Parameters (`*` required) |
|---|---|
| `file_exists` | `path*`, `kind` (`file`/`dir`), `min_bytes` |
| `dir_exists`, `file_absent` | `path*` |
| `file_moved` | `src*`, `dst*` |
| `file_contains` | `path*`, `text*`, `ignore_case` |
| `files_exist` | `base*`, `files*` |
| `download_complete` | `path*`, `min_bytes`, `max_bytes` |
| `browser_url` | one of `contains`, `equals`, `regex` |
| `browser_title` | one of `contains`, `equals` |
| `browser_text` | `contains*`, `selector` |
| `browser_element` | `query*`, `role`, `present` |
| `window_exists` | one of `title_contains`, `title`, `process`; `present` |
| `element_state` | one of `query`, `automation_id`; `window_title`, `window_process`, `role`, `expect{exists, enabled, value_contains}` |
| `value_check` | `actual*`, `op` (`equals`, `contains`, `not_contains`, `regex`, `gt`, `gte`, `lt`), `expected*` |

Any spec may add `wait_s` (0–30).

## Polling (`run_checker`)

Step 7 measured ~30 s per verification attempt when a condition could never become true
(`browser_text` with a `wait_s: 30` on content that is not on the page). The old loop slept a
fixed 0.4 s until the full `wait_s` elapsed and had no notion of "this cannot change".

Current behaviour:

1. **Immediate first check.** No sleep before it. Without `wait_s` there is exactly one check.
2. **Early success exit** as soon as the condition holds.
3. **Early terminal-failure exit.** The outcome is marked `terminal` and never polled when waiting
   cannot change it: unknown kind, invalid parameters, a path outside the allowed roots, no open
   browser page, desktop verification unavailable, an uncomparable `value_check`.
4. **`value_check` never polls** — it compares recorded values.
5. **Bounded wait.** `wait_s` is capped by `limits.verification_max_wait_s` (default 10 s);
   only `download_complete` may use the full 30 s.
6. **Backoff**: 0.15 → 0.3 → 0.6 → 1.0 s between polls.
7. **Settled-state exit** for browser checks: each returns a fingerprint of what it observed;
   when it has not changed for 3 s the page is settled and polling stops.

A step whose failed checks are all `terminal` fails with `plan_invalid` instead of
`verification_failed`, so recovery skips "refresh and re-check" and the adaptive loop and goes to a
single re-plan.

Final verification is skipped entirely when a required step already failed or never ran: the
outcome is known, so nothing is polled.

## What `verified` means

`verified` is computed in one place, `TaskRunner._verification_status`, and is `true` only when:

* the task is `COMPLETED`, and
* there is at least one result and every result passed with `method == "deterministic"`, and
* every required step succeeded, and
* the requested answer data is complete.

| `verification_status` | When |
|---|---|
| `VERIFIED` | all of the above |
| `JUDGED` | completed on an LLM judgement (no deterministic contract) |
| `INCOMPLETE` | criteria passed but an answer field was missing |
| `FAILED` | a criterion did not hold |
| `NOT_RUN` | the task ended before final verification |

Final criteria come from `plan.success_criteria`. When a plan has none, the latest deterministic
result of each step that is still part of the outcome (`SUCCEEDED`, not superseded) is used — the
failed check of a step that a re-plan replaced no longer counts. A re-plan can add success
criteria but cannot remove ones that are still checkable.

## Metrics

Per task, in `UsageStats`: `verification_attempts` (contracts evaluated), `verification_polls`
(checker invocations), `verification_duration_ms`, `verification_success_ms` (time spent in checks
that passed), `verification_failures`. Each `VerificationResult` carries `polls`, `duration_ms`
and `terminal`.

## Limits

* The settled-state exit can stop before a page that updates itself more than 3 s after it last
  changed; a step that needs that must be followed by an explicit wait/read step.
* `verified` proves the declared criteria, not that the criteria fully express the request.

## Tests

`backend/tests/unit/test_verification_polling.py`, `test_truthful_answers.py` (invariants),
`test_vision_and_verification.py`, `test_verification.py`.
