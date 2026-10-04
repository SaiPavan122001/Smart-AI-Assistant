# Architecture

This document describes the system **as implemented**. Sections link to deeper documents in [`docs/architecture`](docs/architecture)
and to the decisions behind them in [`docs/adr`](docs/adr).

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
  tools/          Tool base, registry, router (the only path to the computer), filesystem/terminal/browser/desktop/vision/
                  verification tools, perception cache usage
  perception/     ObservationCache (per-task cache, capability versions, duplicate suppression)
  verification/   checkers (declarative contracts) + engine
  recovery/       error-code → strategy table, bounded, reasoned
  planner/        prompts, plan generation, deterministic plan validation + repair
  agent/          StepExecutor (execute → observe → evaluate → recover), AdaptiveRunner (bounded LLM tool loop), RunState/TaskHandle
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
  args that do not fit the tool's input schema, forward/unknown references, unknown verification kinds, > 25 steps.
* **Steps** are `tool` (fixed tool + args; **zero LLM calls at run time**) or `adaptive` (a bounded LLM tool-calling loop for steps
  whose next action is unknowable up front). Arguments can reference earlier outputs (`{{steps.s3.artifacts.path}}`), resolved
  by deterministic code.
* **Execute** goes through the router (§4). **Evaluate** = the tool succeeded *and* the step's verification contracts hold.
* **Recover** (`recovery/engine.py`) picks from a table keyed by structured error code (stale ref → refresh; element missing → alternative
  selector → adaptive → re-plan; command failed → adaptive fix loop → re-plan; timeout → retry only if idempotent …). Denials, injection
  blocks, limits and cancellation are *never* recovered. Every attempt has a reason, a counter and a budget.
* **Verify** runs `success_criteria` deterministically. If a request has no machine-checkable contract (e.g. "summarize"), a clearly
  labelled lower-assurance LLM judgement is used and the result carries `verified=false`.

More: [docs/architecture/agent-loop.md](docs/architecture/agent-loop.md).

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
  plus `FAILED | CANCELLED | TIMED_OUT`. Terminal states are final; illegal transitions raise.
* **Event sourcing for the UI**: every important thing is an `ExecutionEvent` with a per-task sequence number and correlation IDs
  (`request_id, session_id, task_id, plan_id, step_id, tool_call_id`). The UI rebuilds the timeline purely from events; WebSocket clients
  resume with `?after_seq=N`. See [docs/architecture/realtime-events.md](docs/architecture/realtime-events.md).
* **PostgreSQL** (SQLAlchemy 2 async + Alembic): `users, sessions, tasks, plans, task_steps, agent_actions, tool_calls, observations,
  permissions (policy + decision audit), approvals, execution_logs, verification_results, task_events`. A repository port with three
  implementations (memory, SQLite, PostgreSQL) shares one contract test suite. See [docs/architecture/state-model.md](docs/architecture/state-model.md).

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
