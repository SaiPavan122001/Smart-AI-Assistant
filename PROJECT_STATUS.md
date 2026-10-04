# Project status

_Last full validation run: 2026-10-04, Windows 11 (10.0.22631), Python 3.12.10, Node 22.18, Chrome/Edge 154, PostgreSQL 16.2 (embedded `pgserver`)._

## P0 execution-loop bug (2026-10-04) — status

**Root-cause defects fixed and deterministic tests pass; real-world computer validation pending.** The agent could repeat side-effecting actions
(re-launch an app after a verification false-negative, via adaptive fallback or a replan; duplicate tasks from a double submit with 2 concurrent runtimes).
Added a per-task action ledger with idempotent replay and loop detection (`LOOP_DETECTED`), state-checked launches, idempotent task start and
`max_concurrent_tasks=1`. The incident itself left no persisted record, so its exact sequence is unproven. Details: [docs/execution-loop-fix.md](docs/execution-loop-fix.md).
Live validation (`validation.live`) is paused until this is verified manually.

## Live smoke status (2026-10-04, after the loop fix)

Calculator PASS; Chrome navigation PASS (answer "The page title is 'Example Domain'"); Chrome search PASS via the new `web_search` provider; a Google-in-browser
task hit the bot check, was stopped as `blocked_by_site`, re-planned and completed through `web_search`. No repeated launches, no loop-guard triggers, durable
SQLite records re-open correctly. Fixes made: `browser_text` also checks the page title; planner catalog now shows legal enum/range values; plans can return
deterministic answers (`answer_template`); SearchRouter + blocked-page detection ([docs/web-search.md](docs/web-search.md)).
Not yet done: the broader single-user validation (scenarios A-M2) and the final classification. DuckDuckGo is best-effort; add a Brave/Tavily key for reliability.

## Verdict

**Feature-complete and engineering-ready; not yet validated against a live LLM.** Everything that can be verified without an API key has been
verified, with the model's decisions scripted so the *real* runtime, tools, permission engine, persistence and UI are what is under test.
Before you rely on it for real work, run the checklist in [Before you release](#before-you-release) — in particular a live-model smoke test, because
planner/adaptive prompt quality against a real model is the one major thing that could not be measured here.

## What exists

| Area | State |
|---|---|
| Backend (`backend/app`) | ~15k lines, 120 modules, strict-typed; agent runtime, planner, tool router, permission engine, 6 tool capabilities (40+ tools), recovery, verification, PostgreSQL persistence + Alembic, REST + WebSocket API, observability |
| Frontend (`frontend/`) | React + TypeScript dashboard (task input, live timeline, approvals, STOP AGENT, history, details, logs, permissions, settings) |
| Evaluation suite (`backend/evals`) | 17 deterministic benchmark workflows with regression thresholds |
| Docs | root docs, 11 ADRs, architecture + security + developer guides, generated tool catalogue and OpenAPI (kept in sync by tests) |
| Infra | PostgreSQL compose file, PowerShell scripts (`setup/dev/run/check`), GitHub Actions workflow |

## Validation results (all run in this repository)

| Check | Result |
|---|---|
| `ruff format --check`, `ruff check` (backend) | clean (162 files) |
| `mypy` (strict, pydantic plugin) | **0 issues in 117 source files** |
| Backend tests | **503 passed, 2 skipped** (see below) in ~2.5 min |
| — adversarial/security | 86 command cases, 27 injection cases (attacks, obfuscation, benign controls), path/URL/redaction matrices |
| — real browser (Playwright + installed Chrome) | 18 passed |
| — real Windows UI Automation | read-only tests pass; opt-in test drove a **real Notepad** (found a dropped-keystroke issue → typing now verified by read-back with slower retries) |
| — PostgreSQL | repository contract suite + migration drift/reversibility pass on **real PostgreSQL 16.2**, SQLite and memory; real server booted with auto-migrate, restart-persistence verified |
| — end-to-end agent scenarios | 19 passed (search→download→folder→move, project create/run, failure→recovery→verify, approve/deny, injection from web and local file, STOP while running, pause/resume, re-plan, limits, timeouts) |
| — architecture | package layering is an acyclic stack, enforced by a test (four package-level cycles were found and removed during final review) |
| Evaluation suite (`python -m evals.run`) | **17/17 pass; thresholds met**: completion, tool-selection, execution, verification-correctness, recovery = 100%; unnecessary LLM calls 0; vision calls 0; **security violations 0; permission bypasses 0; injection resistance 100%**; total 46 LLM calls for 17 workflows (a search→download→folder→move task uses exactly 1) |
| Frontend `eslint`, `tsc`, `vitest` | clean · **34 passed** |
| Frontend production build | OK (212 kB JS, 67 kB gzip) |
| Playwright UI E2E (real Chrome, real backend) | **8 passed** |
| Generated docs in sync | yes (`python -m app.docs_gen --check`); `.env.example` verified against the settings model |

Skipped tests: one needs symlink privileges (the Windows *junction* escape test ran and passed), one is the opt-in visible-Notepad test (`SAA_UIA_INTERACTIVE=1`; it was run manually and passed).

## Bugs found by the test/verification process and fixed

Real defects, found *because* the behaviour was exercised end to end (not hypothetical): redactor masked `passed` as a password field; fuzzy target matching clicked a wrong element
for a nonsense query (and "Text editor" matched the "Edit" menu on real Notepad); injected text naming a victim file slipped past phrase-only provenance (now resource-aware); popup tabs were not registered in time;
path resolution raised on unreadable locations instead of denying; a cancelled approval reported as "denied"; cancelled tool calls were missing from the audit trail; recovered steps hid `exit_code` from later references;
keystrokes were silently dropped by real Notepad; the SPA fallback served `index.html` for unknown API paths on Windows; first-run DB failure printed a raw traceback; UIA password-field values were not withheld on the real backend; four package import cycles.

## Not verified / known gaps

1. **No live LLM run.** Anthropic and OpenAI-compatible providers are verified against recorded wire formats (`httpx.MockTransport`), not live endpoints (no API keys here). Planner and adaptive-loop prompt quality with a real model is unmeasured; expect to iterate on prompts and to find plan shapes the validator rejects (it repairs once, then fails clearly).
2. **Vision with a live vision model** — logic, confidence gating, metering and coordinate mapping are tested with a scripted vision model only.
3. **Docker was not available**, so `infra/docker/docker-compose.yml` (a stock `postgres:16-alpine` service) was not executed; the same schema/migrations were validated on PostgreSQL 16.2 via `pgserver`.
4. **Playwright's bundled Chromium** could not be downloaded in this environment (CDN timeout); all browser work used installed Chrome/Edge. `playwright install chromium` is the documented fallback and is untested here.
5. **Real VS Code / Electron apps** were not driven; the "open VS Code" scenario uses the in-memory desktop adapter (clearly labelled test adapter). Electron apps often expose sparse UI Automation trees — the vision fallback exists for that but is untested live.
6. **CI workflow not executed** (no remote); it mirrors `scripts/check.ps1`, which was run locally in pieces.
7. No load/soak testing, no log/audit retention policy, no per-user auth, no TLS, no API rate limiting (see [SECURITY.md](SECURITY.md) limitations 1–9).
8. Desktop automation is Windows-only. Terminal allowlist is intentionally small (no pipes/redirects).
9. No `LICENSE` file — choose one before distributing.

## Before you release

1. `.\scripts\setup.ps1`, set a real `SAA_SECURITY__API_TOKEN` and an LLM key, `python -m app doctor` → all OK.
2. **Live smoke test**: run the three example tasks from the README against your model and provider; check the timeline, approvals, and that the final state is verified. Try an injection page of your own.
3. `.\scripts\check.ps1` (full) green on your machine; confirm `docker compose` PostgreSQL works if you use it.
4. Review [SECURITY.md → Hardening checklist](SECURITY.md#hardening-checklist-for-a-real-deployment) and narrow `SAA_FILESYSTEM__ALLOWED_ROOTS`.
5. Decide on a license; tag the release.

## Recommended next improvements

1. **Live-model evaluation mode** (`python -m evals.run --live`): the same 17 workflows against a real provider, scored on world state, to track real-world plan quality, cost and recovery rates over time.
2. Sandbox approved child processes (Windows Job Objects / AppContainer; restricted token) and optional per-task working copies.
3. Rate limiting and per-client tokens; optional TLS/reverse-proxy guidance; tamper-evident audit (hash chain).
4. Retention/archival for `task_events`, `tool_calls`, `observations`; table partitioning by month.
5. Richer desktop coverage: window-scoped keyboard focus checks before key events, Electron accessibility enabling, MSAA/legacy bridge, clipboard tool with taint semantics.
6. Streaming LLM responses and prompt-caching of tool catalogues; per-step model routing (cheap model for adaptive loops, stronger for planning).
7. Optional MCP *adapter* that registers external tools into the same registry/router (never around it).
8. Packaging: signed Windows installer / `pipx`-style distribution; auto-update.
