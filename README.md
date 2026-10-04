# Smart AI Assistant

**Tell the AI what you want done on your computer, and it figures out how to do it — inside a permission-controlled, verifiable, stoppable system.**

```
"Open Chrome, search for the latest Spring Boot documentation, download the PDF, create a folder called
 Spring Docs on my desktop, and move the PDF there."
```

The assistant understands the request, makes an execution plan, picks the *cheapest reliable* way to do each step
(API → structured data → browser DOM → Windows UI Automation → deterministic input → vision → OCR), executes it through
explicit tool contracts, observes the result, recovers from failures, asks you before anything risky, **verifies the real
state of your computer**, and shows you the complete history.

It is a modular monolith: Python 3.11+ / FastAPI backend, React + TypeScript UI, PostgreSQL, Playwright, Windows UI Automation.

> **Status:** feature-complete and heavily tested (see [PROJECT_STATUS.md](PROJECT_STATUS.md) for exactly what was verified,
> what was not, and known limitations). Read [SECURITY.md](SECURITY.md) before pointing it at a machine you care about.

---

## Why it is built this way

| Principle | What it means here |
|---|---|
| **LLM = reasoning, code = execution** | The model plans, picks tools, recovers, and judges. Predictable steps run as *deterministic tool calls with no LLM involved*. A "create folder, download, move" task costs **one** LLM call (planning). |
| **Structured perception first** | Browser: DOM + accessibility tree + targeted element lookup (`find "Download"` → `button ref=e42`). Desktop: Windows UI Automation. Screenshots/vision are an explicit, metered fallback — never a loop. |
| **Permission engine is the security boundary** | Every call passes `Tool Router → Permission Engine → Tool`. No path from model text to the OS bypasses it. |
| **External content is data, never instructions** | Web pages, files, PDFs, terminal output, window text are fenced, scanned, and *taint* the task; actions derived from them are blocked. |
| **Never claim success without verification** | Declarative verification contracts (file exists, source gone + destination present, URL/title/text, window state, …) are checked against the real world. Unverifiable results are labelled as such. |
| **Stoppable** | One-click **STOP AGENT** engages a process-wide kill switch: running commands are killed, pending approvals denied, queued actions can never start. |

## Architecture at a glance

```
React UI ──REST/WebSocket──▶ FastAPI ──▶ Task Runtime ──▶ Planner (LLM, structured output, validated)
                                              │
                                              ▼
        Step Executor ──▶ Tool Router ──▶ Permission Engine ──▶ Tools ──▶ your computer
          │  ▲                 │ (risk assessment, taint check, approval, limits, redaction, audit)
          │  └── Recovery ◀─── Evaluation ◀── Observation (structured; cached; de-duplicated)
          ▼
   Verification Engine ──▶ TaskResult (verified | not verified) ──▶ PostgreSQL + event timeline
```

Details: [ARCHITECTURE.md](ARCHITECTURE.md) · [docs/architecture/](docs/architecture) · [ADRs](docs/adr).

## Quick start (Windows)

Prerequisites: Windows 10/11, Python 3.11+, Node 20+, Chrome or Edge (or `playwright install chromium`), an LLM API key.
PostgreSQL is optional for a first try (`memory://` keeps nothing on disk).

```powershell
git clone <this repo>; cd "Smart AI Assistant"
.\scripts\setup.ps1                      # venv, dependencies, backend\.env, npm install, environment check

# 1. configure: edit backend\.env
#    SAA_SECURITY__API_TOKEN=<any long random string>      (python -c "import secrets;print(secrets.token_urlsafe(32))")
#    SAA_LLM__API_KEY=sk-...                               (or ANTHROPIC_API_KEY / OPENAI_API_KEY)
#    SAA_DATABASE__URL=memory://                           (quick try)  — or PostgreSQL, see below

# 2a. production-style: builds the UI, migrates, serves UI + API on http://127.0.0.1:8000
.\scripts\run.ps1

# 2b. development: API with reload (8000) + Vite dev server (5173)
.\scripts\dev.ps1            # add -Db to start PostgreSQL with docker compose
```

Open the UI, paste the API token, and give it a task. `python -m app doctor` (run from `backend\`) tells you what is missing.

### PostgreSQL

```powershell
docker compose -f infra/docker/docker-compose.yml up -d --wait
# backend\.env
#   SAA_DATABASE__URL=postgresql+asyncpg://saa:saa@localhost:5432/saa
#   SAA_DATABASE__AUTO_MIGRATE=true        (or run: python -m app migrate)
```

### Try these tasks

* *Open my Downloads folder, find Java-related PDFs, create a Java folder, organize them, and summarize the documents.*
* *Create a Python project called expense_tracker in my workspace with the required files, run it, and fix any errors.*
* *Open Chrome, search for the latest Spring Boot documentation, download the PDF, create a folder called Spring Docs on my desktop, and move the PDF there.*
* *Delete old-backup.zip from my Documents* — watch the specific approval dialog, then try **Deny**: the file stays.

## Features

* **Agent core** — provider-agnostic LLM interface (Anthropic and any OpenAI-compatible endpoint), structured plans validated
  before execution (tools exist, args fit the schema, references resolve), deterministic + adaptive steps, bounded retries,
  re-planning, resource limits (time, actions, LLM/vision calls, retries, tabs, shell operations), pause/resume/cancel.
* **Tools** — 40+ typed tools in six capabilities; see the generated [tool catalogue](docs/tools/catalog.md).
* **Browser agent (Playwright)** — isolated context per task, DOM/aria perception, ref-based targeting with stale-ref detection and
  re-resolution, popups/tabs, downloads (HTTP API first, click fallback, SSRF-guarded redirects), upload disabled by default,
  per-page state cache, screenshots only through the vision tools.
* **Windows desktop agent (UI Automation)** — window enumeration, structured control tree, find by automation-id/name/role,
  invoke/value/select patterns (background-safe: no mouse movement needed), verified keyboard fallback, vision-gated coordinate
  clicks, terminals/credential managers/security prompts are untouchable.
* **Filesystem** — allowlisted roots, blocked system/credential locations, traversal/UNC/ADS/device-name/junction defenses,
  extension and size limits, Recycle-Bin deletes.
* **Controlled terminal** — no shell, ever: commands are tokenized, classified, policy-checked, argument-validated and run with a
  scrubbed environment, timeouts and output caps.
* **Permissions** — risk levels, approvals (once / for this task), trusted operations, per-tool overrides, full audit trail.
* **Prompt-injection defense** — deterministic detection, structural fencing, taint tracking, resource-provenance blocking.
* **Verification & recovery** — explicit contracts; error-code-driven recovery table with reasons and budgets.
* **Observability** — structured JSON logs with correlation IDs and mandatory secret redaction, Prometheus metrics, LLM/vision/tool
  accounting and cost estimates, per-task execution logs.
* **UI** — live event-sourced timeline, specific approval dialogs, always-visible STOP AGENT, history, task details, logs, permissions, settings.

## Platform support

| Capability | Windows 10/11 | macOS / Linux |
|---|---|---|
| API, UI, planner, filesystem, terminal, verification, database | ✅ | ✅ (not a supported target; untested) |
| Browser automation (Playwright; Chrome, Edge or Chromium) | ✅ | ✅ |
| Desktop automation (UI Automation) | ✅ | ❌ tools are disabled |

## Security in one paragraph

The agent can control a computer, so the API binds to loopback, requires a bearer token (compared in constant time), rejects
foreign `Host`/`Origin` headers (DNS-rebinding and cross-site WebSocket defense), and keeps the token out of URLs and logs.
Tools are the only way to act; tool calls cannot bypass the permission engine; filesystem access is allowlisted; there is no shell;
secrets are masked everywhere they could be persisted; external content cannot give instructions. Full detail, threat model and
**honest limitations** in [SECURITY.md](SECURITY.md).

## Testing

```powershell
.\scripts\check.ps1          # format, lint, strict mypy, all tests, eval suite, frontend lint/types/tests/build, Playwright E2E
.\scripts\check.ps1 -Fast    # skip browser/E2E/eval suites
```

* Backend: unit, adversarial security, router/permission pipeline, real Playwright browser tests against a local site, real Windows UI
  Automation tests, repository-contract tests on **memory, SQLite and real PostgreSQL**, migration drift/reversibility, real-server
  boot tests, 19 end-to-end agent scenarios, and a **deterministic evaluation suite** with regression thresholds
  (`python -m evals.run`).
* Frontend: Vitest unit/component tests and Playwright E2E against the real backend.

See [docs/development/testing.md](docs/development/testing.md).

## Configuration

Everything is an environment variable with the `SAA_` prefix — see [`.env.example`](.env.example) (a test keeps it in sync with the
settings model). Key groups: runtime, security, LLM, vision, database, filesystem policy, browser, terminal, desktop, limits.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `python -m app doctor` reports an LLM issue | Set `SAA_LLM__API_KEY` (or `ANTHROPIC_API_KEY`/`OPENAI_API_KEY`). Without a provider tasks fail immediately with a clear message. |
| UI says "Cannot reach the agent backend" | Start the backend (`scripts\run.ps1` / `scripts\dev.ps1`); in dev the UI proxies `/api` to `127.0.0.1:8000`. |
| 401 / login loop | The token must equal `SAA_SECURITY__API_TOKEN` (or the one printed at startup if unset in dev). |
| WebSocket closes immediately (code 1008) | Wrong token, or the page origin is not in `SAA_SECURITY__ALLOWED_ORIGINS`. |
| "Could not launch a browser" | Install Chrome/Edge, or `python -m playwright install chromium`, or set `SAA_BROWSER__CHANNEL=msedge`. |
| Navigation blocked: "private/loopback" | Intentional SSRF guard. For local sites set `SAA_BROWSER__ALLOW_PRIVATE_NETWORK=true`. |
| Path blocked: "outside the allowed folders" | Add the folder to `SAA_FILESYSTEM__ALLOWED_ROOTS`. |
| Command denied: "not on the allowlist" | By design. Permitted programs: see [docs/security/shell.md](docs/security/shell.md); extras via `SAA_TERMINAL__EXTRA_ALLOWED_PROGRAMS` (always with approval). |
| Desktop tools missing | Windows only; `pip install pywinauto`; check `python -m app doctor`. |
| Typing into an app produces garbled text | The app dropped keystrokes; the agent detects this by reading the value back and retries slower, then fails honestly. |
| "Emergency stop engaged" | Press **Release stop** in the header (or `POST /api/v1/agent/release`). |

## Repository layout

```
backend/   Python package `app` (api, agent, planner, orchestration, tools, permissions, security, perception, state,
           verification, recovery, llm, observability, models, database, config, utils), tests, evals, alembic
frontend/  React + TypeScript UI, Vitest tests, Playwright E2E
docs/      architecture, security, ADRs, API (generated OpenAPI), tools (generated catalogue), development guides
infra/     docker-compose for PostgreSQL
scripts/   setup / dev / run / check (PowerShell)
```

## License

No license file has been chosen yet — add one before distributing.
