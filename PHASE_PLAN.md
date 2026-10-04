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

## Suggested next increments

See "Recommended next improvements" in [PROJECT_STATUS.md](PROJECT_STATUS.md).
