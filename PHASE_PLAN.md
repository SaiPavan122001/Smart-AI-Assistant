# Phase plan

Mapping of the delivery phases to what exists in the repository. Status: ✅ implemented and tested · ◐ implemented with a documented limitation · ⏭ future.

| # | Phase | Status | Where | Verified by |
|---|---|---|---|---|
| 0 | Foundation: docs, config, logging, errors, test infra, lint/types/format | ✅ | root `*.md`, `docs/`, `app/config`, `app/observability`, `pyproject.toml`, CI | `scripts/check.ps1`, strict mypy, ruff |
| 1 | LLM + agent core: contracts, provider interface, planner, runtime, router | ✅ | `app/models`, `app/llm`, `app/planner`, `app/orchestration`, `app/agent` | unit + e2e |
| 2 | Tool system: capability tools with full metadata, registry, router | ✅ | `app/tools` · [catalogue](docs/tools/catalog.md) | router/tool tests, docs-sync test |
| 3 | Browser agent (Playwright): DOM/aria perception, refs, state cache, downloads, tabs | ✅ | `app/tools/browser` | 18 real-browser tests |
| 4 | Windows desktop agent (UI Automation) with fallback hierarchy | ◐ | `app/tools/desktop` | real UIA read-only tests + opt-in Notepad drive; tool logic via test adapter; Electron-app trees untested |
| 5 | Filesystem control (allowlists, traversal, protected locations, limits, logging) | ✅ | `app/security/paths.py`, `app/tools/filesystem` | path + tool tests |
| 6 | Terminal/shell security (parsed, classified, validated, approved, logged) | ✅ | `app/security/commands.py`, `app/tools/terminal` | 86 adversarial cases + tool tests |
| 7 | Permission engine (policies, risk, approvals, scopes, trusted ops, audit) | ✅ | `app/permissions`, router | 24 router tests, API tests, UI tests |
| 8 | Prompt-injection defense (data/instruction separation, detection, taint, provenance) | ◐ | `app/security/injection.py`, router | adversarial tests, 2 e2e attack scenarios; heuristic by nature |
| 9 | Vision fallback (provider interface, confidence, metering) | ◐ | `app/tools/vision` | tests with a scripted vision model; no live vision provider run |
| 10 | Token/cost optimisation (caching, targeted retrieval, batching, accounting) | ✅ | `app/perception`, router, `app/observability/usage.py` | cache/meter tests, evals (LLM calls per workflow) |
| 11 | Recovery engine (bounded, reasoned strategies) | ✅ | `app/recovery`, `app/agent/executor.py` | table tests + e2e recovery scenarios |
| 12 | Verification engine (explicit contracts) | ✅ | `app/verification` | contract tests, evals |
| 13 | Task state + PostgreSQL (schemas, migrations, audit) | ✅ | `app/state`, `app/database` | contract suite on memory/SQLite/**PostgreSQL**, migration drift tests |
| 14 | Realtime events (WebSocket, correlation, replay) | ✅ | `app/state/events.py`, `app/api/v1/ws.py` | API + frontend tests |
| 15 | Frontend dashboard (task input, timeline, approvals, history, details, logs, permissions, settings) | ✅ | `frontend/` | 34 Vitest + 8 Playwright E2E |
| 16 | Emergency controls and resource limits | ✅ | `app/utils/control.py`, runtime, router | stop tests (API, e2e, UI) |
| 17 | Observability (JSON logs, correlation, metrics) | ✅ | `app/observability` | metrics/logging tests, real-server test |
| 18 | Secrets/credential security (masking, secret-provider abstraction, isolation) | ✅ | `app/utils/redact.py`, `app/config/secrets.py` | redaction/secret tests |
| 19 | Testing (unit, integration, e2e, adversarial) | ✅ | `backend/tests`, `frontend/tests`, `frontend/e2e` | 500+ backend + 34 frontend + 8 UI E2E |
| 20 | Evaluation suite with metrics and regression thresholds | ✅ | `backend/evals` | `python -m evals.run` (17 workflows) |
| 21 | Documentation | ✅ | root docs + `docs/` | docs-sync + env-sync tests |
| 22 | Quality requirements (typing, boundaries, no silent failures, no unrestricted shell/FS) | ✅ | whole repo | strict mypy, ruff, review |
| 23 | Configurability (provider, model, limits, roots, policies, logging…) | ✅ | `app/config`, `.env.example` | env-sync test |
| 24 | API (versioned, validated) | ✅ | `app/api` · [OpenAPI](docs/api/openapi.json) | API tests, docs-sync |
| 25 | Production readiness: format, lint, types, all suites, migrations, build | ✅ | `scripts/check.ps1`, CI | see [PROJECT_STATUS.md](PROJECT_STATUS.md) |

## Single-user hardening iterations (completed)

| Iteration | Scope | Status | Where |
|---|---|---|---|
| Execution-loop fix | action ledger, loop guard, idempotent start | ✅ | [docs/execution-loop-fix.md](docs/execution-loop-fix.md) |
| Step 7 campaign | broader live validation, findings F1–F8 | ✅ | [docs/single-user-validation.md](docs/single-user-validation.md) |
| P0/P1 correctness + efficiency | truthful answers, output-schema validation, verification polling, `verified` integrity, recovery classification, `PAUSED` state, search error contract, official-source check, call accounting | ✅ | [docs/fixes/](docs/fixes), [docs/verification.md](docs/verification.md), [docs/pause-resume.md](docs/pause-resume.md), [docs/search-errors.md](docs/search-errors.md), [docs/agent-efficiency.md](docs/agent-efficiency.md) |
| v0.1.0 gate | full live regression (18 scenarios), final review, checkpoint | ✅ | [docs/releases/v0.1.0-single-user-core.md](docs/releases/v0.1.0-single-user-core.md) |

**Checkpoint: Single-User Core v0.1.0 (`v0.1.0-single-user-core`).** Everything above is done; do not redo it.

## After the checkpoint

Work starts from the checkpoint. The live gate in [PROJECT_STATUS.md](PROJECT_STATUS.md) is the regression set for every phase:
it must keep passing and its numbers are the baseline.

### NEXT-A — Complex single-user workflows  ← implemented on `phase/next-a-complex-single-user-workflows`; live validation incomplete

Status: **READY FOR MORE SINGLE-USER TESTING.** Implemented and covered by deterministic tests: explicit goal extraction and
evaluation, router-enforced restrictions, `fs_find` / `fs_organize` / `fs_read_many`, grounded summaries and sources, `PARTIAL`
outcome and completion report, checkpoints, failed-command evidence for recovery, lazily built (Electron) UI trees. Live validation:
NA1 ran once and was cut off by the model account's spending limit (files verified correct independently), NA2–NA5 did not run.
Details: [docs/next-a/implementation.md](docs/next-a/implementation.md), [docs/next-a/validation-results.md](docs/next-a/validation-results.md).

Remaining NEXT-A work: run `NA1`–`NA5` live and re-run the 18-scenario gate. Both are part of NEXT-F below and are blocked until the
model key's spending limit is raised (`python -m app llm-check` returns `403 Key limit exceeded`).

## Production-reliability roadmap (single user, Windows)

Branch `phase/production-reliability-roadmap`, started from `a300c2a` (the NEXT-A head). One stage at a time; each is committed
separately after its tests pass. The phase letters after NEXT-A were renumbered for this roadmap (decision R0 in
[DECISIONS.md](DECISIONS.md)). Status: ✅ done and tested · ◐ partly done, limits documented · ⏳ not started · ⛔ blocked.

| Stage | Scope | Status | Where |
|---|---|---|---|
| NEXT-B | Restart-safe task execution and durable recovery | ✅ deterministic (829 backend tests, evals, frontend, Playwright, run as sequential groups); no live-model run | [docs/restart-recovery.md](docs/restart-recovery.md) |
| NEXT-C | Security and permission hardening | ⏳ audit done, two test files drafted, no application code | |
| NEXT-D | Transactional file operations, rollback, partial-failure handling | ⏳ | |
| NEXT-E | Reliable Windows application automation, support matrix | ⏳ | |
| NEXT-F | Evidence-based evaluation; live validation of NA1–NA5 and the 18-scenario gate | ⏳ deterministic part · ⛔ live part (model key limit) | |
| NEXT-G | Dashboard usability and task transparency | ◐ recovery panel only (NEXT-B) | |
| NEXT-H | Installable Windows product and diagnostics | ⏳ | |

### NEXT-B — Restart-safe task execution ✅

Checkpoint: an unfinished task survives the death of the process as `INTERRUPTED`; the user resumes, restarts or cancels it; completed
work is not repeated; an action whose outcome is unknown is never repeated without approval.

* Persisted with the task: the explicit goal, plan, step states and outputs, recovery history, refusals, the cancel request; a
  write-ahead record for every side-effecting call (migration `0002`).
* Startup classifies only. In-flight actions are reconciled against the observable state (`Tool.reconcile`).
* Resume goes through the same router, permission engine, approvals, limits, loop guard and STOP; grants do not carry over.
* API `GET /tasks/{id}/recovery`, `POST /tasks/{id}/recover`; dashboard recovery panel.
* Open: no live-model interruption test (blocked with NEXT-F); reconciliation covers file moves/copies/deletes and page loads only;
  the all-in-one check script has not completed in one run on this machine (memory), its parts were run one at a time.

### Stages not started

NEXT-C: one permission path for every execution route, approvals bound to the exact action, Windows path confinement (junctions,
reparse points), terminal execution controls, STOP completeness, redaction, local API exposure, injection tests per content type.
NEXT-D: action journal, undo with precondition checks, staging and verification, rollback that is idempotent and resumable.
NEXT-E: tested application-support matrix, focus / stale-window / unexpected-dialog detection, postcondition verification.
NEXT-F: failure scenarios (restart, unavailable application, network, invalid model output, timeout, ambiguity, injection, forbidden
and contradictory instructions), outcome metrics, redacted run artifacts; then the live gate.
NEXT-G: goal and requirements view, plan and clarification, evidence versus explanation, per-requirement results.
NEXT-H: packaging and installation workflow, environment and configuration checks, diagnostics, upgrade and uninstall notes.

### Later (not started, not part of this roadmap)

Multi-model / provider comparison; multi-user accounts and isolation; a worker per controlled machine; load testing and scaling.
Still open from the earlier backlog: adaptive-loop cost (about one model call per action), answer completeness for free-text answers.

Also deferred, to be placed when prioritised: scheduled/recurring tasks, job-application automation, large-scale research workflows.
