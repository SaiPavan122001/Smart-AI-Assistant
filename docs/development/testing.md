# Testing

```powershell
.\scripts\check.ps1            # everything CI does
.\scripts\check.ps1 -Fast      # skip browser/E2E/eval suites
cd backend; .\.venv\Scripts\python.exe -m pytest -q          # backend only
cd frontend; npm test; npm run test:e2e                      # frontend only
```

## Backend suites (`backend/tests`, 500+ tests)

| Folder | What | Notes |
|---|---|---|
| `unit/` | paths, router + permission pipeline (24), planner/validation/templating, recovery table, state machine, LLM wire formats, filesystem tools, terminal tools, desktop tools (test adapter), vision, verification contracts, redaction, infrastructure (approvals, events, caches, meters, URL policy, config), `.env.example` sync, docs sync, architecture layering | fast, hermetic |
| `security/` | adversarial command classification (86), prompt-injection detection/obfuscation/taint | `-m security` |
| `integration/` | repository contract on **memory/SQLite/PostgreSQL**; migration drift + reversibility on SQLite and PostgreSQL; REST/WebSocket API (auth, host/origin, approvals, emergency stop, replay); real Playwright browser tests against a local site (18); real UIA tests; real-server boot (`python -m app serve` + PostgreSQL + restart persistence) | PostgreSQL via embedded `pgserver` (skipped if unavailable) |
| `e2e/` | 19 agent scenarios through the real runtime with a scripted model; the evaluation suite as a gate | `-m e2e` |

Markers: `browser`, `windows`, `security`, `e2e`. The Windows desktop test that drives a real Notepad window is opt-in: `SAA_UIA_INTERACTIVE=1`.

### Test architecture

* `tests/helpers.py::make_env` builds the real stack (router, permission engine, approvals, repo, bus) over temp folders; `make_harness` adds the real planner/runner/runtime with a
  `ScriptedProvider`; `Responder` plays the human for approvals; `tests/helpers_tools.py` provides controllable tools for pipeline tests.
* `evals/site.py` is a local website (search form, downloads, SPA, injection page, forms, cookies) used by browser tests and evals.
* Only the **model** is scripted. Everything else is production code. Test adapters (`ScriptedProvider`, `FakeDesktopBackend`, `InMemoryRepository`) are labelled as such and never selectable by configuration.

## Evaluation suite (`backend/evals`)

`python -m evals.run [--only injection] [--json report.json] [--list]` — 17 benchmark workflows with ground-truth checks, scored on: task completion, tool selection, execution (world state),
verification correctness (never over-claims), recovery success, unnecessary LLM calls, vision usage, latency, estimated cost, **security violations, permission bypasses, injection resistance**.
`evals/thresholds.json` defines the regression gate (exit code 1 on breach). It also runs inside pytest (`tests/e2e/test_evals.py`). Deletions in evals are sandboxed (never the real Recycle Bin).

## Frontend

* Vitest + Testing Library (34 tests): event reducer, API client (auth, errors, 401 handling), WebSocket stream (subprotocol auth, dedupe, resume, reconnect, no hammering on 1008),
  approval dialog (specific content, safe focus, scopes, taint warning), STOP AGENT (one click), timeline, task input, result card.
* Playwright E2E (8 tests) against the real backend in `backend/evals/e2e_server.py` (scripted model, sandboxed folders), using installed Chrome (`PW_CHANNEL=msedge` to switch).

## What the tests deliberately do *not* cover

Live LLM endpoints (no keys in CI), real VS Code/Electron accessibility trees, destructive real-desktop interactions, Docker (not available in the dev environment), non-Windows desktop.
