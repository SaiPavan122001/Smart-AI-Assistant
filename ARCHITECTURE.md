# Architecture

This document describes the system **as implemented**. Sections link to deeper documents in [`docs/architecture`](docs/architecture)
and to the decisions behind them in [`docs/adr`](docs/adr).

**Release:** Single-User Core v0.1.0. One user, one machine, one task at a time. Nothing here is multi-user or distributed.

```
User ─▶ React dashboard ─▶ REST / WebSocket API ─▶ Task Runtime ─▶ Planner (LLM → validated plan)
     ─▶ Step Executor ─▶ Tool Router ─▶ Permission Engine (+ approval) ─▶ Tool ─▶ Observation
     ─▶ Verification ─▶ final TaskResult (status · verification_status · answer_status)
```

## 1. Shape: a modular monolith with hard internal boundaries

One Python process (FastAPI) hosts everything; modules depend inward only through explicit interfaces (`Protocol`/ABC + dependency
injection from one composition root, `app/container.py`). No Redis/Kafka/microservices: they would add failure modes without solving
a problem this product has ([ADR-0001](docs/adr/0001-modular-monolith.md)).

Layering is enforced by `tests/unit/test_architecture.py` (packages form an acyclic stack; imports only point downwards):
`utils → models, config → observability, perception, recovery, security, permissions, state → llm, database → tools → verification → planner → agent → orchestration → api`.

```
app/
  models/         Domain contracts (Pydantic): Task, Plan, PlanStep, ToolSpec/Call/Result, Observation, ApprovalRequest,
                  ExecutionEvent, VerificationSpec/Result, TaskResult.   (depends on utils only)
  utils/          ids, structured errors, secret redaction, cancel/pause/kill-switch primitives, text matching, plan-reference templating
  config/         Pydantic settings (env-driven, secrets as SecretStr)
  llm/            LLMProvider ABC, provider adapters (openai, anthropic, gemini, openrouter, openai_compatible), registry, capability registry (httpx), gateway (budget, redaction, accounting,
                  structured-output repair), scripted test adapter
  security/       PathPolicy, command classifier, URL/SSRF policy, injection scanner + taint guard, entity extraction
  permissions/    PermissionPolicy, PermissionEngine (pure decision function), ApprovalManager
  search/         SearchProvider adapters (Brave, Tavily, DuckDuckGo) and SearchRouter (ordered fallback, attempts recorded)
  tools/          Tool base, registry, router (the only path to the computer), filesystem/terminal/browser/desktop/search/vision/
                  verification tools, output contracts (schema.py), action ledger + loop guard, perception cache usage
  perception/     ObservationCache (per-task cache, capability versions, duplicate suppression)
  verification/   checkers (declarative contracts) + engine
  recovery/       error-code → strategy table, bounded, reasoned
  planner/        prompts, plan generation, deterministic plan validation (references.py: field paths + types) + one repair
  agent/          StepExecutor (execute → observe → evaluate → recover), AdaptiveRunner (bounded LLM tool loop), RunState/TaskHandle,
                  answer.py (final answer decided from final state)
  orchestration/  TaskRunner (lifecycle), TaskRuntime (concurrency, pause/cancel/STOP)
  state/          task state machine, repository port, in-memory repository, EventBus, per-task log sink
  database/       SQLAlchemy models, SqlRepository, Alembic migrations
  observability/  JSON logging + correlation, Prometheus metrics, usage meter
  api/            FastAPI app, /api/v1 routers, WebSocket, errors, auth deps, SPA serving
  registry.py     default tool registry assembly
  container.py    composition root · cli.py (serve/migrate/doctor/tools) · docs_gen.py (generated docs)
```

## 2. The agent loop

```
Understand ─▶ Plan ─▶ Validate plan ─▶ Select capability ─▶ Permission check ─▶ Execute ─▶ Observe ─▶ Evaluate
     ▲                                                                                              │
     └──────────── Re-plan ◀── Recover ◀── (retry / refresh / alternative / adaptive) ◀──────────────┤
                                                                       continue ◀──────────────────┘
                                          Verify ─▶ Complete (verified) | Fail (honest report)
```

* **Plan** — one structured LLM call produces `PlanDraft`. `validate_plan` rejects (and the planner repairs once) unknown tools,
  args that do not fit the tool's input schema, forward/unknown references, references to output fields the tool does not declare,
  type mismatches, invalid verification kinds/parameters, contradictory success criteria, > 25 steps.
* **Steps** are `tool` (fixed tool + args; **zero LLM calls at run time**) or `adaptive` (a bounded LLM tool-calling loop for steps
  whose next action is unknowable up front). Arguments can reference earlier outputs (`{{steps.s3.artifacts.path}}`), resolved
  by deterministic code.
* **Execute** goes through the router (§4). **Evaluate** = the tool succeeded *and* the step's verification contracts hold.
* **Recover** (`recovery/engine.py`) picks from a table keyed by structured error code (stale ref → refresh; element missing → alternative
  selector → adaptive → re-plan; command failed → adaptive fix loop → re-plan; timeout → retry only if idempotent …). Denials, injection
  blocks, limits and cancellation are *never* recovered. Every attempt has a reason, a counter and a budget.
* **Verify** runs `success_criteria` deterministically. If a request has no machine-checkable contract (e.g. "summarize"), a clearly
  labelled lower-assurance LLM judgement is used and the result carries `verified=false`.

* **Plan contracts are checked before execution.** Every tool declares its output as a pydantic model (`Tool.Output`); the planner's
  catalogue, the validation of `{{steps.sN.data.x}}` references (existence and type) and the router's contract check all derive from
  it. Verification parameters are validated against the checker's parameter model. An invalid plan gets one repair round, then
  `plan_invalid`. See [docs/fixes/f1-planner-output-schema.md](docs/fixes/f1-planner-output-schema.md).
* **The final answer is derived from final state**, never from what a model said happened: `TaskResult` carries `status`/`outcome`,
  `verification_status` and `answer_status` separately, and only a COMPLETED task whose required steps and answer fields are all
  present gets a success answer. See [docs/fixes/f2-truthful-answers.md](docs/fixes/f2-truthful-answers.md) and
  [docs/verification.md](docs/verification.md).

More: [docs/architecture/agent-loop.md](docs/architecture/agent-loop.md) · [docs/agent-efficiency.md](docs/agent-efficiency.md).

## 3. Perception hierarchy

`API → structured data → DOM → accessibility tree → Windows UIA → deterministic automation → vision → OCR`

* Browser: `browser_find` returns a handful of scored candidates with stable refs, not the page. Reads are targeted (`text`, `aria`,
  `elements`, `links`, `forms`), capped, and **de-duplicated** (an unchanged page returns "unchanged"). Identical read-only calls are
  served from a cache until a mutating tool of that capability runs.
* Desktop: UI Automation tree → typed elements with automation ids; poorly-instrumented apps are reported (`quality: poor|empty`)
  with a pointer to the vision fallback.
* Vision is two explicit tools (`vision_capture` with a recorded *reason*, `vision_analyze`) metered by `max_vision_calls`;
  results carry confidence and low-confidence results cannot be acted on.

More: [docs/architecture/perception.md](docs/architecture/perception.md).

## 4. Tool router and permission engine (the security boundary)

```
ToolCall ─▶ [kill switch / pause] ─▶ lookup ─▶ validate args ─▶ refused-before? ─▶ cache
        ─▶ tool.assess() (risk, action text, resource, category, hard deny)   ◀── path/command/URL policy
        ─▶ injection provenance check (args derived from flagged content ⇒ block)
        ─▶ PermissionEngine.decide(spec, assessment, task grants, taint)  ⇒ ALLOW | ASK | DENY
        ─▶ [approval: specific request → human → once/task grant]   ─▶ re-check kill switch, pause, limits
        ─▶ execute (timeout, bounded retry, cancellable) ─▶ redact ─▶ scan untrusted output ─▶ audit/persist/emit
```

Defence in depth: tools re-validate paths/commands *at execution time* (assessment-time checks guard approvals; run-time checks guard races).
See [docs/architecture/permissions.md](docs/architecture/permissions.md) and [docs/security](docs/security).

## 5. State, events, persistence

* **State machine** (`state/machine.py`): `CREATED → PLANNING → EXECUTING ⇄ (WAITING_APPROVAL | RECOVERING | VERIFYING) → COMPLETED`,
  plus `FAILED | CANCELLED | TIMED_OUT`. Any active state can go to `PAUSED` and back ([docs/pause-resume.md](docs/pause-resume.md)).
  Terminal states are final; illegal transitions raise.
* **Event sourcing for the UI**: every important thing is an `ExecutionEvent` with a per-task sequence number and correlation IDs
  (`request_id, session_id, task_id, plan_id, step_id, tool_call_id`). The UI rebuilds the timeline purely from events; WebSocket clients
  resume with `?after_seq=N`. See [docs/architecture/realtime-events.md](docs/architecture/realtime-events.md).
* **PostgreSQL** (SQLAlchemy 2 async + Alembic): `users, sessions, tasks, plans, task_steps, agent_actions, tool_calls, observations,
  permissions (policy + decision audit), approvals, execution_logs, verification_results, task_events`. A repository port with three
  implementations (memory, SQLite, PostgreSQL) shares one contract test suite. See [docs/architecture/state-model.md](docs/architecture/state-model.md).

### Search and vision

* **Search** — `web_search` → `SearchRouter` → configured providers in order (Brave, Tavily, DuckDuckGo). All providers failing is
  `SEARCH_UNAVAILABLE`. A site operated in the browser that shows a bot check raises `SEARCH_AUTOMATION_BLOCKED`: never solved,
  never retried, one re-plan to another source. Officialness of a result is a deterministic domain check.
  See [docs/web-search.md](docs/web-search.md), [docs/search-errors.md](docs/search-errors.md).
* **Vision** — a separate provider setting (`SAA_VISION__*`), used only through `vision_capture` + `vision_analyze`, metered by
  `max_vision_calls`, gated by a confidence threshold.

### Workflows (NEXT-A)

`planner/goal.py` extracts the user's explicit goal (named outputs, source counts, restrictions) by code; it is stored on the plan,
enforced in the router (`GoalSpec.violation`) and evaluated against the real final state by `verification/goal.py`, using what the
router recorded in `tools/observed.py` (URLs returned, files written, text read). `tools/filesystem/workflow.py` adds `fs_find`,
`fs_organize` and `fs_read_many`. `agent/report.py` builds the completion report (steps, requirements, side effects);
`orchestration/checkpoint.py` stores a progress snapshot after every step (not a resume point). Details:
[docs/next-a/implementation.md](docs/next-a/implementation.md).

### Restart recovery (NEXT-B)

A task continues after a process stop from what the running task persisted, not from a checkpoint: the plan with the user's goal, step
states, step outputs (`tasks.durable`) and the tool-call records. A side-effecting call is recorded before it runs, so an action that
was in flight is visible afterwards. `orchestration/recovery.py` classifies it (safe to retry, confirmed done, confirmed not done,
uncertain) using each tool's read-only `reconcile`; the router replays what already happened and the permission engine forces an
approval for what is uncertain. Startup only classifies (`INTERRUPTED`); the user resumes, restarts or cancels. Details and limits:
[docs/restart-recovery.md](docs/restart-recovery.md).

### Loop safety

Per-task action ledger (`tools/loop_guard.py`): an identical state-establishing action that already succeeded is replayed, not
re-executed; repeated identical actions, action cycles and no-progress streaks trip `loop_detected`, after which no tool, recovery or
re-plan runs. Task start is idempotent (same instruction while running, or same `Idempotency-Key`) and `max_concurrent_tasks = 1`.
See [docs/execution-loop-fix.md](docs/execution-loop-fix.md).

## 6. Cost and latency controls

State/observation caches, targeted retrieval, deterministic batching of predictable steps, adaptive-loop context compression, loop
breakers, duplicate-observation suppression, prompt caching on the stable system prompt, per-task accounting of LLM/vision/tool calls,
tokens, latency and *estimated* cost (prices are configurable estimates), and hard budgets that stop runaway agents.

## 7. What was borrowed from Hermes Agent (and what was not)

Used as a *conceptual* reference only; no code was copied and there is no dependency: the computer-use abstraction, accessibility-driven
control with structured UI perception, browser/desktop separation, capability-based tool abstraction, permission/approval concepts and
state-aware execution. UI Automation invoke/value patterns give "background-safe" interaction where the target app supports them
(no mouse movement or focus change); coordinate input is a last resort.

## 8. Explicit non-goals

Multi-user tenancy, remote/headless-cloud desktops, non-Windows desktop automation, and any unrestricted shell/OS access.
