# Agent efficiency: model calls, recovery, budgets

## Execution model

```
LLM -> structured plan -> deterministic tool sequence -> deterministic verification -> rendered answer
```

* A `kind="tool"` step runs with no model call. Its arguments are fixed or resolved from recorded
  outputs (`{{steps.sN.data.x}}`).
* A `kind="adaptive"` step is a bounded tool-calling loop, used only where the next action cannot
  be known at plan time.
* The final answer is rendered from the plan's `answer_template` by code
  (`app/agent/answer.py`). `answer_generation_calls` is always 0: there is no "phrase the answer"
  model call.
* An LLM judgement call (`judge_calls`) happens only for a task with no machine-checkable contract.

## Where the calls went (traced from Step 7)

| Cause | Example | Fix |
|---|---|---|
| Invalid references / verification contracts fail at run time, then refresh → adaptive → re-plan, repeatedly | S2 (10–16 calls), S3 (13), S8a (9) | Static plan validation with one repair ([F1](fixes/f1-planner-output-schema.md)); a run-time contract defect is `plan_invalid` → one re-plan, no refresh/adaptive |
| The failed check of a superseded step kept failing the final verification, forcing more re-plans | S3 | Final verification only counts steps that are still part of the outcome |
| A task-level re-plan could never succeed because the old failed step stayed "blocking" | any lifecycle re-plan | A re-plan supersedes every step that did not succeed |
| Non-retryable conditions were re-planned at task level although the step-level engine had stopped | 9b: two re-plans after "search unavailable" | Shared classification; `NON_RETRYABLE_CODES` never re-plan, `SINGLE_REPLAN_CODES` re-plan once per task |
| Guessed verification values (`browser_title contains " "`, a wrong process name for a launched app) | S3, Calculator | Parameter contracts reject placeholders; the planner is told a launch tool confirms its own result and to assert only what the request or a step output states |
| Adaptive loop: one model call per action, plus a lookup before each action | Calculator: 6 finds + 6 clicks = 10 step calls | The loop accepts several tool calls per response (run in order, rest skipped after a failure); prompt asks for batches and direct action by `query` |
| Verification polled ~30 s per attempt | S4 attempt 1 | See [verification.md](verification.md) |

## Batched adaptive actions

`AdaptiveRunner` executes all tool calls of one model response in order. If one fails, the rest
of that batch is not run (they were chosen assuming the earlier one worked) and a `finish_step` in
the same response is ignored, so the model sees the failure before it can claim success. OpenAI-
compatible providers are sent `parallel_tool_calls: true`.

Whether a model actually batches is up to the model. In the live runs `openai/gpt-4.1` stopped
looking elements up first (6 fewer calls on Calculator) but still sent one click per response.

## Recovery

Classification (`app/utils/errors.py`):

| Class | Codes | Behaviour |
|---|---|---|
| retryable | `timeout`, `navigation_failed`, `io_error`, `stale_reference`, `llm_error` | bounded retry / refresh per step (`max_retries_per_step`) |
| non-retryable | `approval_denied`, `approval_timeout`, `prompt_injection_blocked`, `cancelled`, `kill_switch_active`, `limit_exceeded`, `loop_detected`, `unknown_tool`, `capability_not_available` | step fails, no recovery, no re-plan |
| single re-plan | `policy_denied`, `path_blocked`, `command_blocked`, `url_blocked`, `SEARCH_AUTOMATION_BLOCKED`, `SEARCH_UNAVAILABLE`, `plan_invalid` | no retry; one re-plan per code per task, then final |

Other codes keep their strategy tables (`recovery/engine.py`). A user denial during adaptive
recovery of a deterministic step ends the recovery.

## Budgets

* Global: `limits.max_llm_calls` (default 60), unchanged.
* Task-aware: after planning and after every re-plan, `usage.llm_budget` is set to
  `calls so far + pending adaptive steps × max_adaptive_iterations + min(pending tool steps, 2) ×
  max_adaptive_iterations (recovery allowance) + remaining re-plans × 2 + 1`, capped by the global
  limit. Exceeding it is `limit_exceeded`. A one-step deterministic plan gets 18 with default
  limits; a plan with many adaptive steps gets the global limit.

## What is recorded per task

`UsageStats`: `llm_calls`, `planner_calls`, `replanner_calls`, `repair_calls`, `step_calls`,
`judge_calls`, `answer_generation_calls`, `llm_budget`, `tool_calls`, `recoveries`, `retries`,
`replans`, `vision_calls`, verification and pause counters, tokens, cost.

`TaskResult.metrics` (`TaskMetrics`, also logged as `task_finished`): `task_id`, `provider`,
`model`, `llm_calls`, `planner_calls`, `replanner_calls`, `repair_calls`,
`answer_generation_calls`, `tool_calls`, `recovery_count`, `verification_attempts`,
`verification_duration`, `pause_count`, `resume_count`, `vision_calls`, `task_duration`,
`final_status`, `verified`, `answer_status`. No arguments, outputs or secrets are included.

## Measured (live, `openrouter / openai/gpt-4.1`, 2026-10-04)

Before = Step 7 campaign / smoke runs. After = re-run on the hardened code
(`backend/validation/results/p01-*.json`). Single runs; model output varies between runs.

| Task | Before: status · LLM · tools · re-plans · recoveries · time | After |
|---|---|---|
| Calculator 25 × 16 | COMPLETED · 11 · 14 · 0 · 0 · 23 s | COMPLETED · 7 · 8 · 0 · 0 · 21 s |
| example.com page title | COMPLETED · 2 · – · 0 · 0 | COMPLETED · 2 (1 plan + 1 repair) · 2 · 0 · 0 · 11 s |
| Spring Boot official docs (title + URL) | COMPLETED · 1 · 1 (no officialness check) | COMPLETED · 2 (1 plan + 1 repair) · 1 · 0 · 0 · 10.5 s, domain verified |
| S2 dynamic page | FAILED ×2 · 10 / 16 · 8 / 15 · 2 / 2 · 5 / 10 | COMPLETED · 11 / 14 · 8 / 13 · 0 · 0 / 1 |
| S3 five pages + pause | COMPLETED, answer incomplete · 13 · 16 · 2 · 7 · 71.5 s | COMPLETED, full answer · 1 · 5 · 0 · 0 · 24.6 s |
| S8a page summary (injection) | FAILED · 9 · 7 · 2 · 8 | COMPLETED · 3 · 2 · 0 · 0 |
| S8b page summary (hidden injection) | FAILED · 6 · 3 · 2 · 3 | COMPLETED · 3 · 2 · 0 · 0 |
| S8c file summary (injection) | FAILED · 5 · 2 · 2 · 3 | COMPLETED · 2 · 1 · 0 · 0 |

## Limits

* S2 still costs 11–14 calls: its adaptive step is a real find/click/read loop on a page that
  re-renders, and the model works one action per response.
* The repair round costs one extra call when the first plan is invalid (seen on two simple tasks).
  It replaces a failed task, but it is a call.
* The budget is an upper bound against runaway loops, not a target.
