# Security

Smart AI Assistant can read, write and run things on your computer on your behalf. Treat it like a power tool: understand what it
can do, keep the defaults, and read the limitations below. This document describes the controls **as implemented and tested**; each
section links to the detailed note in [`docs/security`](docs/security).

**Reporting a vulnerability:** open a private security advisory on the repository (do not file a public issue).

## Security model in six rules

1. **The LLM never touches the OS.** It can only name a *tool* and its arguments. Every call is validated, risk-assessed, permission-checked,
   executed under timeouts/limits, redacted and audited by the router. There is no generic "run this" tool.
2. **The permission engine is mandatory.** `LLM → Tool Router → Permission Engine → Tool → Computer`. Hard denies (blocked paths,
   dangerous commands, protected windows, bad URLs) cannot be approved by anyone.
3. **Only the user's request carries intent.** Everything else the agent reads is untrusted *data* (fenced, scanned, taint-tracked).
4. **Risky means asked.** High/critical operations show a *specific* approval (action, reason, resource, risks) and can be denied;
   a denied action is never retried or re-asked in that task.
5. **Secrets stay out.** Masked in logs, events, tool results, task history, approval records and LLM prompts; never sent to the browser.
6. **You can always stop it.** STOP AGENT engages a process-wide kill switch (see [emergency-stop](docs/security/emergency-stop.md)).

## Controls

| Area | Control | Detail |
|---|---|---|
| API | Bearer token, constant-time compare; loopback only, a network address is refused unless explicitly enabled with a chosen token; the WebSocket token is accepted in the subprotocol only, never in the URL; error bodies are redacted; `TrustedHost` + CORS allowlists; WebSocket `Origin` check and token via subprotocol; uniform error bodies; `Cache-Control: no-store`; access logs disabled (they would contain query strings) | prod refuses to start without `SAA_SECURITY__API_TOKEN` |
| UI | Token in `sessionStorage` only; strict CSP in production builds; no secrets rendered; settings are read-only | |
| Filesystem | Allowlisted roots, blocked system/credential locations, symlink/junction resolution, UNC/device/ADS/reserved-name/drive-relative/env-var rejection, blocked executable extensions on write, size limits, Recycle-Bin deletes. Listing, search and folder copy never enter a symlink, junction or mount point, and every search match is checked against the policy itself (credential-looking files are invisible, by name and by content) | [filesystem.md](docs/security/filesystem.md) |
| Terminal | No shell. Tokenizer rejects `& \| ; < > ^ ( ) \` %`; deny patterns (encoded PowerShell, download-and-execute, registry, persistence, credential extraction, recursive delete, disk formatting, security bypass…); program allowlist with sub-command classification; path arguments validated; scrubbed environment; timeout, output cap, process-tree kill; a Windows Job Object ties the command and everything it starts to the call (ended on exit, timeout, STOP and if the agent dies), with optional memory and process limits | [shell.md](docs/security/shell.md) |
| Browser | Fresh isolated context per task; only `http(s)`; SSRF guard (loopback/private/link-local/internal names; redirects re-checked per hop); executable downloads blocked; uploads disabled by default; sensitive-looking clicks and password-field typing need approval and are masked | |
| Desktop | Protected windows (security prompts, credential managers, registry editor…) are invisible and untouchable; **terminals cannot receive input** (it would bypass the shell controls); validated key grammar (no Win key); vision-gated coordinates with confidence threshold; PyAutoGUI fail-safe stays on | |
| Injection | Rule-based scanner (NFKC + zero-width/bidi normalisation), data fencing with nonce, taint tracking that escalates approvals and disables trusted operations/task grants, provenance blocking for resources named in flagged content | [prompt-injection.md](docs/security/prompt-injection.md) |
| Secrets | Key- and value-pattern redaction (API keys, JWTs, connection strings, private keys, `KEY=value`) on every persistence/prompt/event path, including event messages, audit reasons and API error bodies; `SecretStr` config; child processes get no inherited secrets | [credentials.md](docs/security/credentials.md) |
| Limits | Max time, actions, LLM calls, vision calls, retries per step, re-plans, shell operations, adaptive iterations, tabs, concurrent tasks; approval timeout | stops runaway agents |
| Audit | Every permission decision, approval (who/when/scope/note), tool call (redacted args), observation, verification result and event is persisted with correlation IDs | |

## Approvals

An approval is for **one exact action**: it records a fingerprint of the tool and its original arguments, the task, the risk and the
category. It cannot be answered twice, answered after it expired, or used by another task. After you approve, the call is assessed
again; if it would now do something else than what you were shown (replace a file that did not exist, a higher risk, another
resource, a path that became protected) it is **not executed** (`approval_stale`) and has to be asked again.

An approval request always states **what** the agent wants to do, **why**, **which resource** it touches and **what could go wrong**;
the technical arguments (redacted) are one click away. Scopes: *once* or *for this task* (not offered for critical risk, arbitrary code
execution, installs, credential entry, or in tainted tasks). Focus starts on **Deny**; Escape denies. See [approvals.md](docs/security/approvals.md).

## After a restart

A task the service left unfinished is `INTERRUPTED` and does nothing until the user decides ([restart-recovery](docs/restart-recovery.md)).
A resumed task passes the same router, permission engine, goal restrictions, limits, loop guard and STOP as a new one, and in addition:

* approvals do not carry over: a pending request is void, an approval given but not yet acted on is not reused, and a
  *for this task* grant is forgotten, so the action is asked again with its exact arguments;
* a refusal does carry over: an action the user denied is not requested again;
* an action that was in progress and whose outcome is unknown needs an approval for that one call, even if policy, a trusted
  operation or a grant would normally allow it;
* a cancel or STOP is stored before it takes effect, so a task the user stopped is never offered for resume;
* prompt-injection taint is rebuilt from the recorded tool results before the first action.

## Undoing file changes

Every change the filesystem tools make is journalled before it happens, and what an overwrite replaces is kept in
`<data_dir>/journal/` ([file-transactions](docs/file-transactions.md)). A rollback runs only when you ask for it, only for a finished
task, and each undo is an ordinary tool call through the router and the permission engine; the planner and the model cannot undo
anything. An undo touches an item only if it is still exactly as the agent left it, and what it removes goes to the Recycle Bin.

## Known limitations (please read)

These are real gaps, listed so you can decide whether the residual risk is acceptable:

1. **Prompt-injection detection is heuristic.** It is deterministic and tested against many phrasings/obfuscations, but a novel attack can
   evade it. The real backstops are: provenance blocking, taint-driven approvals, the permission engine, and *you* reading approvals.
   A model that is tricked into choosing an action on a resource the page never named will reach an approval dialog flagged as tainted.
   Provenance blocking needs a path, file name or URL in the injected text; an instruction without one ("delete everything") is only
   covered by the taint rules (ask, once, flagged).
2. **Approved code runs with your privileges.** `python main.py`, `npm test`, etc. execute arbitrary code once approved. The Job Object
   around a command limits its lifetime (and, if configured, memory and process count); it is **not** a sandbox: the code can read and
   write whatever you can. The controls are the approval, a scrubbed environment, a restricted working directory and timeouts. Do not
   approve code you would not run yourself.
3. **Input injection into apps that host a terminal** (e.g. VS Code's integrated terminal) cannot be blocked by process name; only
   standalone terminals (`cmd`, PowerShell, Windows Terminal, bash…) are blocked.
4. **SSRF coverage is not total.** Top-level navigations and downloads are DNS-resolved and checked; sub-resource requests are blocked
   for literal private IPs and internal names but are not DNS-resolved per request (DNS-rebinding gap).
5. **Single user, token auth, no TLS.** It listens on loopback and refuses any other address unless `SAA_SECURITY__ALLOW_REMOTE_BIND=true`
   and a token you chose are set. Even then there is no TLS and no rate limiting: put a TLS reverse proxy with its own authentication
   in front. Any program running as your Windows user can read the token from the environment or `.env` and call the API.
6. **Data leaves the machine to the LLM provider**: your instruction, tool outputs (file text, page text, window text) and, if the
   vision fallback is used, screenshots. Redaction is pattern-based and cannot recognise arbitrary free-form secrets. The instruction
   you type is stored as written: do not put a password in it.
7. **Restrictions in the request are enforced by pattern.** "Do not delete anything" is recognised by `app/planner/goal.py` (delete, overwrite,
   move, run commands) and enforced by the router for every tool call, planned or adaptive. An unusual phrasing ("make sure nothing is
   gone") is not recognised; the permission engine and approvals still apply to everything.
8. **Grounding checks are heuristics.** A source URL counts only if a tool returned it, and a saved summary must share most of its
   distinctive words with the text that was read. This catches invented sources and unrelated summaries, not a subtly wrong summary.
9. **VS Code's editor is not controllable.** Through UI Automation VS Code exposes menus, the activity bar, the sidebar and terminal tabs,
   but not the editor text, and input into its terminal is blocked (see 3). Coding workflows use the filesystem and terminal tools.
10. **File names are attacker-controllable data.** `fs_find` / `fs_read_many` results are fenced and scanned like any external content; the
    heuristic detector does not recognise instructions hidden by hyphenating or splitting words in a file name.
11. **Audit rows are not tamper-evident** (an administrator with database access can alter them).
12. **UI Automation reads on-screen content.** Password-like fields are masked by heuristics only.
13. **Only one live model was exercised.** The single-user validation campaigns ran against `openai/gpt-4.1` through OpenRouter. The
    Anthropic, Gemini and plain OpenAI adapters are verified against recorded wire formats only; run a smoke test with your key and
    model before relying on it. See [PROJECT_STATUS.md](PROJECT_STATUS.md).
14. **"Official source" means "on the stated domain".** The check is deterministic, but the domain comes from the request,
    configuration or the planner; see [docs/search-errors.md](docs/search-errors.md).
15. **Path checks are check-then-use.** A path is resolved (links followed) when the risk is assessed and again when the tool runs, and
    directory walks never enter a symlink, junction or mount point. A link swapped in the instant between the second check and the
    file operation is not excluded; it requires another program running as you, at that moment. **Hard links are not detected**: a hard
    link inside an allowed folder to a file outside it is an ordinary file to every check.
16. **A "for this task" approval is broad.** It covers later calls of the same tool and category at the same or lower risk in that task
    (for example further deletes), not only the one shown. Use "once" when that matters. It is never offered for critical actions or in
    a task that read injected content, and it does not survive a restart.
17. **An approval is re-checked, not frozen.** After you approve, the call is assessed again and refused if its risk, category or
    affected resource changed. A change that leaves all three the same (the *content* of a file about to be moved) is not detected.
18. **Job Object limits are per command and optional.** Memory and process limits are off unless configured. A child that a command
    starts before the job is attached (a window of microseconds) would escape it; the timeout tree-kill still applies.
19. **The emergency stop is not persisted.** A restart releases it. Nothing runs by itself after a restart (interrupted tasks wait for
    you), so this does not start work, but new tasks can be submitted again.
20. **The interactive API docs (`/api/docs`) are served without the token.** They describe the API; every operation still needs it.
21. **Journal backups are copies of your files.** What an overwrite replaced stays under `<data_dir>/journal/` (your profile, never sent
    anywhere) until the change is undone; it is not pruned automatically. A deletion is not undone by a rollback (restore it from the
    Recycle Bin), and files written by terminal commands, downloads or desktop applications are not journalled.

## Hardening checklist for a real deployment

* Set `SAA_ENV=prod` and a long random `SAA_SECURITY__API_TOKEN`; keep `SAA_HOST=127.0.0.1`.
* Keep `SAA_TERMINAL__ALLOW_SHELL_INTERPRETERS=false`, `SAA_BROWSER__ALLOW_UPLOAD=false`, `SAA_BROWSER__ALLOW_PRIVATE_NETWORK=false`.
* Narrow `SAA_FILESYSTEM__ALLOWED_ROOTS` to the folders you actually want automated.
* Consider `SAA_SECURITY__BLOCK_ON_INJECTION=true` and setting MEDIUM risk to *ask* (Permissions page) if you work with untrusted content.
* Run under a dedicated, low-privilege Windows account when possible.
* Use a PostgreSQL password that is not the compose default.
