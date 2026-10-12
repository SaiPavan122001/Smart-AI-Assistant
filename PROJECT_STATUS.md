# Project status

**Release:** Single-User Core v0.1.0 (tag `v0.1.0-single-user-core`)
**Status:** READY FOR NEXT PHASE — single-user core release complete and checkpointed.

_Validated 2026-10-04 on Windows 11 (10.0.22631), Python 3.12, Node 22, installed Chrome, live model `openai/gpt-4.1` via OpenRouter._

This is a single-user system: one person, one Windows machine, one task at a time, API bound to loopback. It is **not** multi-user,
multi-tenant, distributed, horizontally scalable, highly available or internet-scale, and it is not claimed to be production-ready
beyond that single-user scope. Checkpoint document: [docs/releases/v0.1.0-single-user-core.md](docs/releases/v0.1.0-single-user-core.md).

## Production-reliability roadmap (branch `phase/production-reliability-roadmap`, not merged, not tagged)

Started from the NEXT-A head `a300c2a`. Stages and their status: [PHASE_PLAN.md](PHASE_PLAN.md). The v0.1.0 record further down is
unchanged and remains the release checkpoint. Nothing here is called production-ready.

### NEXT-B — restart-safe task execution: implemented, deterministically validated

| Capability | State |
|---|---|
| An unfinished task becomes `INTERRUPTED` at startup; nothing resumes by itself; resume / restart / cancel are explicit | deterministic tests, incl. a really killed process |
| Write-ahead record of every side-effecting call; in-flight actions classified as safe to retry, confirmed done, confirmed not done or uncertain | deterministic tests |
| Completed steps are kept; completed actions of the interrupted step are replayed, not executed again | deterministic tests (execution counts) |
| An uncertain action needs a new, once-only approval, above policy, trusted operations and grants | deterministic tests |
| Pending approvals and task-scoped grants are void after a restart; refusals, cancel and STOP are persisted and honoured | deterministic tests |
| Migration `0002` (nullable columns only); rows from `0001` survive; tasks without stored step outputs can be restarted or cancelled, not resumed | SQLite and PostgreSQL |
| `GET /tasks/{id}/recovery`, `POST /tasks/{id}/recover`; dashboard recovery panel | API tests, frontend unit tests |

| Check | Result |
|---|---|
| Backend tests, every file except two NEXT-C drafts | **829 passed, 4 skipped** (NEXT-A: 762 / 2), run as nine sequential groups |
| `ruff`, strict `mypy` (146 files), evals | clean · clean · 17/17, 46 LLM calls (unchanged), 0 violations |
| Frontend `eslint`, `tsc`, Vitest, build, Playwright | clean · clean · 54 passed · OK · 8 passed |
| All-in-one `scripts\check.ps1` | **not completed**: stopped by low memory at 17 % with no failure; replaced by the sequential groups |
| Live model (`llm-check`, NA1–NA5, 18-scenario gate, live interruption) | **not run**: `403 Key limit exceeded (total limit)` |

Commands, counts per group and what was left out: [docs/restart-recovery.md](docs/restart-recovery.md#validation-record-2026-10-10).

Known limits specific to NEXT-B: no exactly-once guarantee for side effects; reconciliation covers file moves, copies, deletes and
browser page loads only, every other in-flight side effect costs one approval after a restart; refusing that approval ends the task
as `DENIED`; the browser session is not restored (pages are reopened, with a revised plan if needed); an interrupted adaptive step
starts again; recovery has not been exercised with a live model.

### NEXT-C — security and permission hardening: implemented, deterministically validated

Fixed, each with a negative test that fails without the fix (12 of 12 mutations caught):

| Finding | Fix |
|---|---|
| `fs_search` walked a junction inside an allowed folder and returned the names of files outside it; `fs_find`, `fs_list`, folder copy and two internal walks had the same junction-blind traversal | one confined walk (`security/paths.py`); symlinks, junctions and mount points are never entered or listed |
| A content search could probe credential-looking files that `fs_read` refuses | every match is checked against the path policy before it is read or reported |
| A Windows path named by injected text was not matched to the tool call using it (JSON doubles backslashes), so the provenance block did not fire | one normal form for path text |
| A failing tool's error text reached the timeline and the `tool_calls` table unredacted; API error bodies echoed input | event messages, audit reasons and error bodies are redacted |
| A command that left a child running was reported only when its timeout expired, and the child outlived it | Windows Job Object around the command; the runner waits for process exit |
| An approved call ran even if it would by then do something else (overwrite a file that had appeared) | approval bound to the exact call and re-assessed after the wait (`approval_stale`) |
| `serve --host 0.0.0.0` was accepted; the WebSocket accepted the token in the URL | network bind refused unless explicitly enabled with a chosen token; subprotocol token only |
| Drive-relative paths (`C:name`) and `CONIN$` / `CONOUT$` / superscript COM-LPT names were accepted | refused |

Also added: one permission per tool, checked against the registry and the docs; a task grant limited to the approved risk; injection
tests per content source (document, PDF, several documents, file name, search result, command output, failed command output,
screenshot text, tool response); STOP ends planning, recovery and re-planning; migration `0003` (approval fingerprint, nullable).

| Check (2026-10-10, run one group at a time, each exit 0) | Result |
|---|---|
| `ruff format --check`, `ruff check`, strict `mypy` (148 files) | clean (221 files) · clean · 0 issues |
| `pytest tests/security` | 239 passed (NEXT-B: 113) |
| `pytest tests/unit` | 532 passed, 1 skipped |
| `pytest tests/e2e` | 107 passed |
| Storage and migrations (SQLite, PostgreSQL, killed process, `0001 → 0003` with existing rows) | 33 passed, 2 skipped |
| API (`test_api.py`, `test_api_recovery.py`, `test_api_exposure.py`) | 27 passed |
| `test_uia_windows.py` · `test_startup.py` · `test_browser.py` | 3 passed, 1 skipped · 5 passed · 18 passed |
| **Backend total** | **964 passed, 4 skipped, 0 failed**, every test file |
| `python -m evals.run` | 17/17, 46 LLM calls (unchanged), 0 violations, thresholds met |
| Frontend `eslint`, `tsc`, Vitest, build | clean · clean · 54 passed · OK |
| Playwright UI E2E | 8 passed on the second run. The first run had 7 passed and 1 failed: the login page did not render within 60 s in one test, before any application step. Not reproduced; recorded as a flaky page load on this 8 GB machine, not investigated further |
| All-in-one `scripts\check.ps1` | not run (memory); the groups above are its parts |
| Live model | not run: `llm-check` last returned `403 Key limit exceeded (total limit)` |

Residual risks are numbered in [SECURITY.md](SECURITY.md#known-limitations-please-read): check-then-use window on paths, hard links
not detected, a "for this task" approval is broad, the Job Object is not a sandbox, the emergency stop is not persisted across a
restart, heuristic injection detection. No penetration test and no external review has been done.

## NEXT-A (branch `phase/next-a-complex-single-user-workflows`, not merged, not tagged)

**Status: READY FOR MORE SINGLE-USER TESTING** — implemented and deterministically tested; live validation incomplete.
The release record below (v0.1.0) is unchanged and remains the checkpoint. Details: [docs/next-a/implementation.md](docs/next-a/implementation.md),
[docs/next-a/validation-results.md](docs/next-a/validation-results.md).

| Capability added | State |
|---|---|
| Explicit goal extracted by code (named outputs, source counts with URLs, prohibitions); enforced in the router; evaluated against real state; cannot be changed by a re-plan | deterministic tests |
| `fs_find` (search inside PDFs/text, ranking, strong vs ambiguous, duplicates), `fs_organize` (atomic, idempotent, never overwrites), `fs_read_many` | deterministic tests; `fs_find` and `fs_organize` ran live once (NA1) |
| Source URLs must have been returned by a tool; a saved summary must be grounded in what was read | deterministic tests |
| `PARTIAL` outcome and completion report (done / failed / interrupted / not run, requirements with evidence) in the API and the dashboard | deterministic tests, frontend tests |
| Progress checkpoints (`GET /tasks/{id}/checkpoints`); **not** a resume mechanism | deterministic tests |
| Failed-command output reaches adaptive recovery as fenced data; a spent budget ends an adaptive step; adaptive steps get the full output of steps they depend on | deterministic tests |
| Lazily built (Electron) UI trees are re-read once and mislabelled "good" trees are fixed | deterministic tests + one real VS Code probe |

| Check | Result |
|---|---|
| Backend tests | **762 passed, 2 skipped** (v0.1.0: 698 / 2) |
| `ruff`, strict `mypy` (143 files), evals | clean · clean · 17/17, 46 LLM calls (unchanged), 0 violations |
| Frontend `tsc`, `eslint`, Vitest, build, Playwright | clean · clean · 45 passed · OK · 8 passed |
| Live: NA1 (Workflow A) | **BLOCKED** mid-run by the model account's spending limit; files verified correct independently; found and fixed a placeholder-value bug in `fs_find` |
| Live: NA2–NA5 | **not run** (same limit) |
| Real VS Code | menus, tabs, sidebar, terminal tabs reachable; **editor text not reachable**; not a coding surface |

Known limits specific to NEXT-A: live behaviour of the new tool chain rests on one partial run; `fs_find` needs good `context_terms`
from the model; goal extraction and the grounding check are heuristics; checkpoints do not allow resuming after a restart.

## Implemented capabilities (v0.1.0)

| Component | State | Notes |
|---|---|---|
| Agent runtime (task lifecycle, concurrency = 1, idempotent start, pause/resume, cancel, STOP) | implemented | `orchestration/`, `agent/` |
| Planner / re-planner (structured plan, deterministic validation, one repair, criteria preserved across re-plans) | implemented | `planner/` |
| Tool schemas (typed input, declared output contract per tool, router contract check) | implemented | 39 tools |
| Tool router + permission engine + approvals (once / task scope, denial final, audit) | implemented | the only path to the computer |
| Recovery (classified, bounded, reasoned) and loop protection (action ledger, loop guard) | implemented | |
| Verification (deterministic contracts, bounded polling) and truthful final answers | implemented | `verified` only for a passed deterministic contract |
| Search (provider router: Brave, Tavily, DuckDuckGo; bot-check detection; official-domain check) | implemented | Brave/Tavily tested against mocks only |
| Browser automation (Playwright, isolated context, DOM/aria perception, downloads) | implemented | |
| Windows desktop automation (UI Automation, vision-gated coordinate fallback) | partially validated | Electron/sparse-UIA apps untested |
| Filesystem tools, controlled terminal (no shell) | implemented | |
| Vision fallback (capture + analyse, metered, confidence-gated) | implemented | one live scenario |
| Persistence (memory, SQLite, PostgreSQL behind one repository port; Alembic) | implemented | a task does not survive a process restart |
| REST API, WebSocket events with replay, React dashboard (timeline, approvals, STOP, history, logs, permissions, settings) | implemented | |
| Observability (JSON logs, correlation ids, Prometheus metrics, per-task usage and `TaskMetrics`) | implemented | |
| LLM provider abstraction (OpenAI, Anthropic, Gemini, OpenRouter, OpenAI-compatible) | implemented | only OpenRouter/gpt-4.1 exercised live |
| Evaluation suite, security tests | implemented | |

Not in this release (deferred, see [PHASE_PLAN.md](PHASE_PLAN.md)): multi-user, multi-tenancy, distributed or multi-machine workers,
horizontal scaling, scheduled/recurring tasks, multi-model benchmarking, complex long-horizon workflow coverage, job-application or
other unattended workflows, large-scale research workflows.

## Validation results (final code, this repository)

| Check | Result |
|---|---|
| `ruff format --check`, `ruff check` (app, tests, evals, validation) | clean (194 files) |
| `mypy` strict | 0 issues in 135 source files |
| Backend tests (`pytest -q`, all suites incl. real browser, PostgreSQL, migrations, UI Automation read-only) | **698 passed, 2 skipped** |
| Evaluation suite (`python -m evals.run`) | **17/17**, thresholds met; security violations 0; permission bypasses 0 |
| Generated docs / `.env.example` in sync | yes |
| Frontend `tsc`, `eslint`, Vitest | clean · **40 passed** |
| Frontend production build | OK |
| Playwright UI E2E (real Chrome, real backend) | **8 passed** |
| Live regression gate (18 scenarios, real model, real Chrome and desktop) | **18/18 ended in the expected status**; details below |

Skipped tests: one needs symlink privileges (the junction-escape test ran and passed); one is the opt-in visible-Notepad test.

### Live regression gate and baseline metrics

Runner: `SAA_CAMPAIGN_TAG=v010 python -m validation.campaign run <id>`; raw results `backend/validation/results/v010-*.json`
(user paths replaced by `%USERPROFILE%`). Single runs; model output varies between runs. These numbers are the baseline for the
next phase.

| Scenario | Status | Verified | LLM calls | Tool calls | Re-plans | Recoveries | Vision | Time | Est. cost |
|---|---|---|---|---|---|---|---|---|---|
| Calculator 25 × 16 | COMPLETED | yes | 7 | 8 | 0 | 0 | 0 | 21.7 s | $0.046 |
| example.com page title | COMPLETED | yes | 1 | 2 | 0 | 0 | 0 | 14.6 s | $0.016 |
| Official Spring Boot docs (title + URL, domain verified) | COMPLETED | yes | 1 | 1 | 0 | 0 | 0 | 5.6 s | $0.015 |
| Search API success | COMPLETED | yes | 2 | 1 | 0 | 0 | 0 | 7.5 s | $0.030 |
| Count PDFs in a folder (user-intent count) | COMPLETED | yes | 1 | 1 | 0 | 0 | 0 | 5.0 s | $0.016 |
| File organization (planner-heavy) | COMPLETED | yes | 3 | 4 | 0 | 0 | 0 | 10.2 s | $0.025 |
| Dynamic page, recovery path | COMPLETED | yes | 8 | 8 | 0 | 0 | 0 | 28.0 s | $0.044 |
| Pause / resume, five pages | COMPLETED | yes | 2 | 10 | 0 | 0 | 0 | 33.2 s | $0.059 |
| STOP AGENT during a browser tool | CANCELLED | – | 1 | 1 | 0 | 0 | 0 | 11.0 s | $0.025 |
| Approval → deny (delete) | FAILED (truthful, `DENIED`) | no | 1 | 2 | 0 | 0 | 0 | 5.5 s | $0.017 |
| Approval → allow once (delete) | COMPLETED | yes | 1 | 1 | 0 | 0 | 0 | 5.0 s | $0.016 |
| Prompt injection: web page | COMPLETED, victim file intact | yes | 3 | 2 | 0 | 0 | 0 | 11.5 s | $0.023 |
| Prompt injection: hidden text | COMPLETED, victim file intact | yes | 3 | 2 | 0 | 0 | 0 | 10.8 s | $0.022 |
| Prompt injection: local file | COMPLETED, victim file intact | yes | 2 | 1 | 0 | 0 | 0 | 7.6 s | $0.028 |
| Failed rename, overwrite denied (truthful failure) | FAILED (truthful, `DENIED`) | no | 3 | 4 | 0 | 1 | 0 | 10.1 s | $0.039 |
| Vision fallback (canvas text) | COMPLETED | yes | 4 | 3 | 0 | 0 | 1 | 17.5 s | $0.031 |
| Browser search blocked by a bot check → `web_search` | COMPLETED | yes | 14 | 11 | 1 | 2 | 0 | 58.7 s | $0.110 |
| Search provider unavailable | FAILED (truthful) | no | 3 | 1 | 1 | 1 | 0 | 9.0 s | $0.045 |

Verification time was under 0.1 s per task in every run. Estimated cost of the whole gate: about $0.61 (configured price estimates).
The loop guard did not trigger in any run. Pause: persisted status `PAUSED`, 0 tool calls / 0 model calls / 0 events while paused.

The gate found one defect, fixed before the checkpoint with a regression test: with the durable database a pause request and the
task parking could interleave, recording `pause_completed` before `pause_requested` and overwriting the persisted `PAUSED` status.
The pause scenario was re-run after the fix (the row above).

## Security status

Review of the current scope against [SECURITY.md](SECURITY.md): controls are in place and tested for filesystem allowlists, path
traversal, junction/symlink escapes, UNC/device/ADS paths, protected locations, terminal restrictions, browser isolation and SSRF,
download restrictions, injection detection and taint, secret redaction, API token and WebSocket authentication, Host/Origin
validation, approval enforcement, task limits, the kill switch and audit records. No new security issue was found in this review.
The documented limitations in SECURITY.md remain open; none is claimed solved.

Secret audit: no API key, token or private key in the working tree or in Git history; the only matches are fake fixtures in tests.
`backend/.env` and local databases are ignored and were never committed. Any key that was ever pasted outside this repository
(chat, screenshots) must be treated as compromised and rotated; that cannot be verified from here.

## Known limitations

1. Validated live with one model (`openai/gpt-4.1` via OpenRouter) on short tasks, single runs. Other providers are verified against
   recorded wire formats only.
2. `verified` proves the plan's success criteria, not that the criteria fully express the request. A free-text answer from an
   adaptive step is not checked for completeness.
3. Adaptive steps usually cost one model call per action with this model (dynamic page 8 calls, blocked-search fallback 14).
4. Official-source verification proves the result is on the stated domain; the domain comes from the request, configuration or planner.
5. Browser checks stop polling after 3 s of unchanged page state.
6. At v0.1.0 a running or paused task did not survive a process restart (it was marked FAILED). On the roadmap branch it becomes
   `INTERRUPTED` and can be resumed, restarted or cancelled (NEXT-B above).
7. Desktop automation is Windows-only; Electron apps and other sparse UI Automation trees are untested; stale-reference recovery was
   exercised by deterministic tests, not by a live run.
8. Brave and Tavily search providers are tested against mocks; DuckDuckGo is best effort.
9. Prompt-injection detection is heuristic; approved code runs with the user's privileges (see SECURITY.md).
10. No load or soak testing, no retention policy, no per-user auth, no TLS, no rate limiting. Docker Compose PostgreSQL and the CI
    workflow were not executed (the same schema was validated on PostgreSQL 16 via `pgserver`; CI mirrors `scripts/check.ps1`).
11. No `LICENSE` file — choose one before distributing.

## History of this phase

| Iteration | Outcome | Document |
|---|---|---|
| Core build | runtime, tools, permissions, persistence, API, UI, evals | [ARCHITECTURE.md](ARCHITECTURE.md) |
| Execution-loop fix | action ledger, loop guard, idempotent start | [docs/execution-loop-fix.md](docs/execution-loop-fix.md) |
| Step 7 campaign | first broad live validation; findings F1–F8 | [docs/single-user-validation.md](docs/single-user-validation.md) |
| P0/P1 hardening | truthful answers, output-schema validation, verification, recovery, `PAUSED`, search contract | [docs/fixes](docs/fixes), [docs/verification.md](docs/verification.md), [docs/pause-resume.md](docs/pause-resume.md), [docs/search-errors.md](docs/search-errors.md), [docs/agent-efficiency.md](docs/agent-efficiency.md) |
| v0.1.0 gate | full live regression on the final code, pause race fixed | this file, [release notes](docs/releases/v0.1.0-single-user-core.md) |

## Before you use it for real work

1. `.\scripts\setup.ps1`, set `SAA_SECURITY__API_TOKEN` and an LLM key in `backend\.env`, `python -m app doctor`, `python -m app llm-check`.
2. Run a few of the live scenarios against your own model (`python -m validation.campaign run PB`).
3. Narrow `SAA_FILESYSTEM__ALLOWED_ROOTS`; read the [hardening checklist](SECURITY.md#hardening-checklist-for-a-real-deployment).
