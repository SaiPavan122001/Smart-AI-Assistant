# The agent loop

`orchestration/runner.py::TaskRunner._lifecycle` drives one task; `agent/executor.py::StepExecutor` drives one step.

## Lifecycle

| Phase | Status | What happens | LLM? |
|---|---|---|---|
| Understand + Plan | `PLANNING` | `Planner.create_plan` → structured `PlanDraft` → `validate_plan` → (one repair round) → `Plan` | 1 call |
| Execute | `EXECUTING` | steps run in order; a step is skipped if any step it depends on (explicit `depends_on` **or** referenced via `{{steps.sN…}}`) failed/was skipped | 0 for `tool` steps |
| Approval | `WAITING_APPROVAL` | the router asks the human; the previous phase is restored afterwards | 0 |
| Recover | `RECOVERING` | strategy chosen by the recovery table; may delegate to an adaptive loop or re-plan | 0–n |
| Verify | `VERIFYING` | step contracts after each step; `success_criteria` at the end; LLM judgement only if there is *no* deterministic contract | 0–1 |
| Finish | `COMPLETED / FAILED / CANCELLED / TIMED_OUT` | `TaskResult` with honest summary, verification evidence, artifacts, usage | 0 |

## Plans

`PlanStep` fields: `id, title, kind (tool|adaptive), tool, args, goal, allowed_tools, depends_on, rationale, verify[], optional` plus runtime
state (`status, attempts, summary, error, output`). `Plan` adds `summary, assumptions, success_criteria, version`.

Argument references: `{{steps.s3.data.stdout}}`, `{{steps.s2.artifacts.path}}`, `{{steps.s1.summary}}`. A whole-string reference keeps its type; embedded
references are stringified; only recorded step outputs are addressable (no environment, no attributes). A step's own verification may reference
its own output (e.g. `download_complete` on `{{steps.s3.artifacts.path}}`).

## Step execution

```
attempt:  [resolve templates] → router.execute(tool call)  | adaptive loop
evaluate: ok? → run step.verify contracts (status VERIFYING) → pass → STEP_COMPLETED
fail:     RecoveryEngine.decide(code, phase, history, budgets) →
            RETRY            same call after backoff (only if idempotent or the failure happened before any side effect)
            REFRESH_RETRY    drop caches, re-read structured state, retry (verification failures: re-check only, never re-run the action)
            ALTERNATIVE      deterministic alternative selector (drop stale ref → relax role → most distinctive word)
            ADAPTIVE         bounded LLM loop with the error in context (never nested)
            REPLAN           planner revises the remaining work; failed step is marked superseded
            FAIL             stop safely
```

Error classification lives in `app/utils/errors.py` and is shared by the recovery engine and the runner:

* `RETRYABLE_CODES` (`timeout`, `navigation_failed`, `io_error`, `stale_reference`, `llm_error`) — bounded retry / refresh.
* `NON_RETRYABLE_CODES` (`approval_denied/timeout`, `prompt_injection_blocked`, `cancelled`, `kill_switch`, `limit_exceeded`,
  `loop_detected`, `unknown_tool`, `capability_not_available`) — never recovered and never re-planned, at step level or at task level.
* `SINGLE_REPLAN_CODES` (policy/path/command/url blocks, `SEARCH_AUTOMATION_BLOCKED`, `SEARCH_UNAVAILABLE`, `plan_invalid`) — no retry;
  exactly one re-plan per code per task, a second occurrence is final.

A reference or verification contract that cannot be resolved at run time is `plan_invalid` (one re-plan), not a retryable failure.
When a deterministic step is recovered adaptively and the user denies an action, recovery ends with `approval_denied`.
A re-plan can add success criteria but cannot drop ones that are still checkable. Each attempt appends a `RecoveryAttempt(reason, error_code, phase)`
and emits `recovery_started` with the reason (the UI shows it).

## Adaptive loop

`AdaptiveRunner.run`: system prompt (trust model + rules) + goal + compact progress; tools = the step's `allowed_tools` plus a pseudo-tool
`finish_step`; `tool_choice="any"` so the model must act or finish. Every call goes through the router. Guards: identical-call repeat limit
(2 for mutations, 3 for reads), old tool results compressed to a short head, iteration cap, the global LLM budget. When it recovers a *tool* step
it exposes the last successful call of the planned tool as the step's `data/artifacts`, so later references still resolve.

## Re-planning

`Planner.replan` receives the instruction, completed-step summaries, the problem, a state summary and the denial memory ("the user refused N
actions; do not propose them again"). New steps are appended with fresh ids; remaining pending steps are marked *superseded*. Bounded by `max_replans`.

## Final verification

* If `success_criteria` exist → deterministic checks; any failure → one more re-plan round (bounded) else `FAILED` with the failing evidence.
* Else if steps carried verification → the latest result per check decides.
* Else (pure Q&A/summaries) → `llm_judgement` result, `verified=false`.

A task is `COMPLETED` only if there are no blocking failed steps and verification passed. `TaskResult.verified` is true only for deterministic passes.
