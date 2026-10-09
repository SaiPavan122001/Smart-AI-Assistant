# NEXT-A — Complex single-user workflows: implementation

Branch `phase/next-a-complex-single-user-workflows`, started from `v0.1.0-single-user-core` (`68ac773`). Scope: make the existing
agent complete bounded, realistic, multi-step tasks reliably and report them truthfully. Nothing here is multi-user, distributed,
scheduled or production-hardened (see [Deferred](#deferred)). Live results are in [validation-results.md](validation-results.md).

## 1. What already existed, and what was missing

The release already had the planner and re-planner, structured tool schemas, reference validation, success-criteria merging across
re-plans, the action ledger and loop guard, bounded recovery, pause / STOP / approvals, SearchRouter and the truthful answer logic.
Those were reused unchanged. The gaps were found by reading the code and then confirmed by tests that failed before the change:

| Requirement of the phase | State at v0.1.0 | Done in NEXT-A |
|---|---|---|
| Structured goal with requirements, constraints and prohibitions | Only model-written `success_criteria`; nothing the model forgot could be caught | `GoalSpec` extracted **by code** from the request (§2) |
| Requirements survive every re-plan | Criteria merged, but only the ones the model wrote | The goal is fixed for the task; re-plans cannot touch it |
| "Do not delete anything" is a hard rule | Only a sentence in the prompt | Enforced in the router and rejected at plan time (§3) |
| Requested count / named output file / "saved, not just said" | Unchecked unless the model wrote a criterion | Evaluated against real state at the end (§2) |
| Sources are real, summaries are grounded | Not checked | URLs must have been returned by a tool; summary vocabulary must come from what was read (§4) |
| Recursive discovery, ranking, ambiguity, duplicates | `fs_search` matched names / small text files only | `fs_find` (§5) |
| Move or copy a list of files, create the folder | One step or one model call per file | `fs_organize`, atomic validation, idempotent (§5) |
| Read several documents | One call per file | `fs_read_many` (§5) |
| Truthful partial results | `FAILED` only; progress not reported | `PARTIAL` outcome and a completion report (§6) |
| Checkpoints during a task | None | Step-boundary progress snapshots (§7) |
| Failure diagnosis for a coding task | Recovery saw `"Command exited with code 1"` | The failed command's stdout/stderr reach recovery as fenced data (§8) |
| Stop when a budget is spent | Adaptive loop kept asking the model for another way | Adaptive step ends on `LIMIT_EXCEEDED` (§8) |

## 2. The explicit goal (`app/planner/goal.py`, `app/models/goal.py`, `app/verification/goal.py`)

`extract_goal(instruction)` is deterministic: the same words always give the same `GoalSpec`. It recognises, conservatively (it prefers
missing a requirement to inventing one):

* **Named output files** — a file name or path that follows a write verb (`save … as Java-summary.md`). An input file ("read notes.txt")
  is not an output. A save request without a name ("save the comparison to a Markdown file") requires *some* file written by the task.
* **Number of sources with URLs** — "three resources … title and URL of each" ⇒ 3 distinct source URLs, in the saved file when there is
  one, else in the answer; "the URL of the top result" ⇒ 1; "from spring.io" restricts which URLs count.
* **A grounded summary** — summarise/compare + a saved file ⇒ the file must be drawn from what the task actually read.
* **Prohibitions** — `do not / never / without` + delete, overwrite, move, run commands. A restriction does not leak across a conjunction
  ("don't delete anything, and move the PDFs" forbids only the delete).

The goal is stored on the plan (`Plan.goal`), shown to the planner as an `<explicit_goal>` block, and **evaluated at the end against the
real state** by `evaluate_goal`. Each requirement is `met`, `unmet` or `unverified`; `unverified` is never reported as met. The results are
stored as `goal:*` verification results, so they take part in `verified` exactly like the plan's own criteria. A failed requirement
becomes the re-plan's problem statement ("verification 'goal:artifact' failed: no file named Java-summary.md was saved by this task").

## 3. Restrictions are enforced, not requested

* `ExecutionContext.goal` carries the goal; `ToolRouter` refuses any call that `GoalSpec.violation` flags (a forbidden tool, or any
  `overwrite=true`) with `GOAL_VIOLATION` **before** assessment, permission decision or approval. The user is never asked to approve what
  their own request forbids. It applies to planned steps, adaptive steps and recovery alike.
* `validate_plan_detailed(..., goal=)` rejects a plan whose steps (or `allowed_tools`) use a forbidden tool, so the one repair round tells
  the model what to change. `GOAL_VIOLATION` is a single-re-plan code (like `POLICY_DENIED`); the outcome is `DENIED` with its own wording.

## 4. What the task has seen and written (`app/tools/observed.py`)

The router records, after every successful tool call, into `ctx.observed`: URLs returned, files written by side-effecting filesystem
tools, text returned by observation tools (bounded to 400 000 characters) and the list of successful side effects. This is the ground truth
the goal evaluation uses:

* a URL counts as a *source* only if a tool returned it (`urls_in` / `normalize_url`); an invented URL fails the requirement even if the page exists;
* a summary is *grounded* when at least 55 % of its distinctive words (≥ 4 letters, a stop-word list removed, URLs stripped) occur in the observed text. It is a
  heuristic, documented as one: it catches an unrelated or empty summary, not a subtly wrong one;
* a path the user named must have been written during the task (an old file with that name does not count).

## 5. New tools (`app/tools/filesystem/workflow.py`)

| Tool | Purpose | Safety properties |
|---|---|---|
| `fs_find` | Files **about** terms: searches inside PDFs and text, ranks, classifies each candidate `strong` or `ambiguous`, reports same-name duplicates and byte-identical copies, filters by extension, name, date, size | read-only, inside allowed roots, credential-looking files invisible, cooperative STOP, bounded (50 000 files scanned, 150 opened, 25 MB each) |
| `fs_organize` | Move / copy a **list** of files into one folder (created if needed) | the whole batch is validated before the first file moves; never overwrites or deletes; identical twin ⇒ skipped; different file with the same name ⇒ skipped or renamed (`on_conflict`); re-running is safe (`already_in_place`, `dest_paths`) |
| `fs_read_many` | Read up to 20 files/PDFs in one call, bounded per file and in total | one unreadable file is reported, not fatal; untrusted output |

**Ambiguity is conservative by construction.** One word can mean two things ("Java" the island), and code cannot tell. `fs_find` therefore
*requires* `context_terms` (words a document genuinely about the topic also contains). A document that mentions the term but none of the
confirming words, or mentions excluded words at least as often, or whose text could not be read, is `ambiguous`; `strong_paths` is the only
list a plan should act on. This was found by a test: without the requirement the island travel guide was classified `strong`.

## 6. Truthful partial results (`app/agent/report.py`, `app/models/report.py`)

`TaskResult.report` is built from the final state only: steps completed / failed / **interrupted** (was running when the task ended — its
action may have completed) / not run, each requirement with its status and evidence, the successful side effects, re-plans and recoveries.

`TaskOutcome.PARTIAL` (status stays `FAILED`) is used only when the task changed something **and** at least one stated requirement holds in
the real final state **and** at least one does not. A failure with no stated requirement stays `FAILED` (the existing contract). `verified`
is never true and the answer is `WITHHELD`; the failure text lists what was done, what failed, what did not run and what remains unchecked,
and says that completed actions were not undone.

## 7. Checkpoints (`app/orchestration/checkpoint.py`)

After every step, after every re-plan and at the end, a checkpoint is stored as an agent action (`kind="checkpoint"`, no migration) and
announced as `checkpoint_saved`. It holds task / plan ids, step statuses, completed and remaining step ids, outstanding requirements,
recovery history, approval counts, file paths written, the side-effect list and usage. It holds no step output, page text, document text,
credential or secret (titles and summaries pass through `redact`). `GET /api/v1/tasks/{id}/checkpoints` returns them.

**A checkpoint is not a resume point.** Nothing reads it to continue a task; a task still does not survive a process restart. That is
deferred to the production-hardening phase.

## 8. Orchestration changes

* An adaptive step that declares `depends_on` receives those steps' output in full (16 000 characters each, fenced as untrusted data). Before,
  it saw a 1 200-character excerpt, which is not enough to summarise documents.
* Adaptive recovery of a failed deterministic step receives the failed action's own output (`stdout`/`stderr`, 4 000 characters) as a fenced
  `failed_action_output` block — never in the goal text — so a failing test run can be diagnosed from the failing assertion.
* A tool result with `LIMIT_EXCEEDED` ends the adaptive step at once; the model is not asked for another way around a spent budget.
* Planner prompt: rule 13 (a–f) describes the workflow shapes above; the adaptive prompt requires summaries to use only text returned by tools.

## 9. Interface

* API: `GET /tasks/{id}/checkpoints`; `TaskResult.report`; `outcome` may be `PARTIAL`; event `checkpoint_saved`.
* Dashboard: a `PARTIAL` badge, a progress panel (requirement status with evidence, steps done / failed / interrupted / not run), one
  updating "Progress checkpoint" timeline line, "Requirements met / not met" separate from plan verification, "Task only partly completed".

## 10. Files

New: `app/models/goal.py`, `app/models/report.py`, `app/planner/goal.py`, `app/verification/goal.py`, `app/tools/observed.py`,
`app/tools/filesystem/workflow.py`, `app/agent/report.py`, `app/orchestration/checkpoint.py`, `validation/next_a.py`,
`tests/workflow_fixtures.py`, `tests/unit/test_goal.py`, `tests/unit/test_fs_workflow_tools.py`, `tests/e2e/test_complex_workflows.py`,
`tests/e2e/test_research_and_coding_workflows.py`.
Changed: `app/tools/router.py`, `app/tools/context.py`, `app/planner/planner.py`, `app/planner/prompts.py`, `app/orchestration/runner.py`,
`app/agent/{answer,executor,adaptive,state}.py`, `app/models/{enums,plan,task}.py`, `app/utils/errors.py`, `app/registry.py`,
`app/api/v1/tasks.py`, `frontend/src/{types/api.ts,features/tasks/*}`, generated docs.

## 11. Deterministic tests added

| File | Tests | What they pin down |
|---|---|---|
| `tests/unit/test_goal.py` | 22 | extraction (named files, input vs output, counts, URLs, domains, prohibitions and their reach), router enforcement, requirement evaluation (empty file, stale file, invented / insufficient / off-site URLs, ungrounded summary, path probing outside roots) |
| `tests/unit/test_fs_workflow_tools.py` | 11 | strong / ambiguous classification, duplicates, identical copies, context / exclude terms, metadata filters, root + credential policy, atomic validation, idempotent re-run, rename / copy, bounded multi-read, observed ledger |
| `tests/e2e/test_complex_workflows.py` | 15 | Workflow A end to end (with a real PDF workspace); Workflow C; missing save caught and re-planned without repeating the move; chat-only summary never a success; PARTIAL; forbidden tool at plan time and at run time; checkpoints and secrets; STOP mid-workflow; action budget; bad reference repaired; pause between steps |
| `tests/e2e/test_research_and_coding_workflows.py` | 8 | Workflow B (three grounded sources; invented URL; too few sources; provider unavailable); Workflow D (real `unittest` run behind approval, diagnosis from real output, unfixable failure, denial is final, workspace roots) |

Mutation checks made while writing them: disabling the goal evaluation fails 9 workflow tests; disabling router enforcement fails 3.

## Deferred

Process-restart recovery / durable workers, resume from a checkpoint, multi-model comparison, real VS Code driving (see
[validation-results.md](validation-results.md#vs-code)), large-scale research, scheduling, anything multi-user or distributed.
