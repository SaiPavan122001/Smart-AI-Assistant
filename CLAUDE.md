# CLAUDE.md

Guidance for coding sessions in this repository.

## Where the project stands

**CURRENT RELEASE: Single-User Core v0.1.0** (tag `v0.1.0-single-user-core`).

**NEXT WORK: begin the next phase only from this checkpoint.** The backlog is in [PHASE_PLAN.md](PHASE_PLAN.md).

**NEXT-A (complex single-user workflows) is implemented on branch `phase/next-a-complex-single-user-workflows`** (not merged, not tagged).
Read [docs/next-a/implementation.md](docs/next-a/implementation.md) and [docs/next-a/validation-results.md](docs/next-a/validation-results.md)
first. Live validation of NA1–NA5 is **incomplete** (the LLM key's spending limit was reached); resume with `NA1`, see PHASE_PLAN.md.

**DO NOT redesign or redo the completed current phase.** The decisions it rests on are indexed in [DECISIONS.md](DECISIONS.md);
change one only with a new entry that says why.

Read these first, in this order; together they say what is done, tested, not done, deferred, and where to continue:

1. [docs/releases/v0.1.0-single-user-core.md](docs/releases/v0.1.0-single-user-core.md) — the checkpoint
2. [PROJECT_STATUS.md](PROJECT_STATUS.md) — capabilities, validation results, limitations, baseline metrics
3. [PHASE_PLAN.md](PHASE_PLAN.md) — next-phase backlog
4. [DECISIONS.md](DECISIONS.md) — decisions and why
5. [ARCHITECTURE.md](ARCHITECTURE.md), [SECURITY.md](SECURITY.md)

## What this is

A permission-controlled Windows computer-use agent for **one user on one machine**: Python 3.11+ / FastAPI backend (`backend/app`),
React + TypeScript dashboard (`frontend`), Playwright browser automation, Windows UI Automation, SQLite or PostgreSQL.

It is not multi-user, multi-tenant, distributed, horizontally scalable or highly available, and has no scheduled tasks. Do not
describe or build those as part of the current release.

## Commands

Run backend commands from `backend/` with the project virtualenv (`backend\.venv\Scripts\python.exe`).

```powershell
.\scripts\check.ps1 -Fast                      # format, lint, strict mypy, unit/security/API tests, frontend lint/types/tests/build
.\scripts\check.ps1                            # everything, incl. browser, PostgreSQL, evals, Playwright UI tests

python -m pytest -q                            # backend tests (all)
python -m ruff format app tests evals validation ; python -m ruff check app tests evals validation
python -m mypy                                 # strict, configured in pyproject.toml
python -m evals.run                            # deterministic evaluation suite with thresholds
python -m app.docs_gen                         # regenerate docs/tools/catalog.md and docs/api/openapi.json (a test checks they are current)

npm run typecheck ; npm run lint ; npm test ; npm run build ; npm run test:e2e      # from frontend/
```

Live validation (real model, real Chrome and desktop, sandbox folders under `~/SmartAIAssistant/validation`; needs `backend/.env`):

```powershell
python -m validation.campaign list
$env:SAA_CAMPAIGN_TAG = "v010"; python -m validation.campaign run PA      # results: backend/validation/results/<tag>-<id>.json
$env:SAA_CAMPAIGN_TAG = "nexta"; python -m validation.campaign run NA1    # NEXT-A workflows NA1..NA5 (disposable data under ~/SmartAIAssistant/validation/next_a)
python -m validation.tool_probe vscode                                   # read-only look at an open VS Code window through UI Automation
```

## Rules that must keep holding

* Everything that touches the computer is a tool and runs through `ToolRouter` → `PermissionEngine`. No shortcuts, not even in tests.
* Every tool declares `Output` (and `Artifacts` if it emits any). A plan may only reference declared fields. Changing a tool's output
  means changing its model; the router fails a test run on drift.
* A task result is success only when `status == COMPLETED`; `verified` only when the deterministic contract passed. Never render a
  success answer for any other outcome. The answer is decided in `app/agent/answer.py`, nowhere else.
* A re-plan cannot relax success criteria. A user denial is final. A bot check is never bypassed or retried.
* External content (pages, files, search snippets, terminal output, window text) is untrusted data.
* The user's explicit goal (`app/planner/goal.py`: named outputs, source counts, prohibitions) is extracted by code, stored on the plan and
  never edited by a model or a re-plan. Prohibitions are enforced in `ToolRouter`; requirements are evaluated against real state
  (`app/verification/goal.py`, `app/tools/observed.py`). A source URL is real only if a tool returned it.
* `PARTIAL` is an outcome (status stays `FAILED`): `verified` is never true and the answer is withheld. Never relabel it as success.
* A checkpoint is a progress snapshot, not a resume point. Do not claim a task survives a process restart.
* Every bug fix comes with a regression test. Keep `python -m evals.run` thresholds green.
* Generated docs and `.env.example` must stay in sync with the code (tests enforce both).

## Never commit

`backend/.env` or any real key, local databases (`*.db`, `data/`), browser profiles, personal files. Validation result JSON is
committed only with user paths replaced by `%USERPROFILE%`.
