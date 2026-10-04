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
| API | Bearer token, constant-time compare; loopback bind by default; `TrustedHost` + CORS allowlists; WebSocket `Origin` check and token via subprotocol; uniform error bodies; `Cache-Control: no-store`; access logs disabled (they would contain query strings) | prod refuses to start without `SAA_SECURITY__API_TOKEN` |
| UI | Token in `sessionStorage` only; strict CSP in production builds; no secrets rendered; settings are read-only | |
| Filesystem | Allowlisted roots, blocked system/credential locations, symlink/junction resolution, UNC/device/ADS/reserved-name/env-var rejection, blocked executable extensions on write, size limits, Recycle-Bin deletes | [filesystem.md](docs/security/filesystem.md) |
| Terminal | No shell. Tokenizer rejects `& \| ; < > ^ ( ) \` %`; deny patterns (encoded PowerShell, download-and-execute, registry, persistence, credential extraction, recursive delete, disk formatting, security bypass…); program allowlist with sub-command classification; path arguments validated; scrubbed environment; timeout, output cap, process-tree kill | [shell.md](docs/security/shell.md) |
| Browser | Fresh isolated context per task; only `http(s)`; SSRF guard (loopback/private/link-local/internal names; redirects re-checked per hop); executable downloads blocked; uploads disabled by default; sensitive-looking clicks and password-field typing need approval and are masked | |
| Desktop | Protected windows (security prompts, credential managers, registry editor…) are invisible and untouchable; **terminals cannot receive input** (it would bypass the shell controls); validated key grammar (no Win key); vision-gated coordinates with confidence threshold; PyAutoGUI fail-safe stays on | |
| Injection | Rule-based scanner (NFKC + zero-width/bidi normalisation), data fencing with nonce, taint tracking that escalates approvals and disables trusted operations/task grants, provenance blocking for resources named in flagged content | [prompt-injection.md](docs/security/prompt-injection.md) |
| Secrets | Key- and value-pattern redaction (API keys, JWTs, connection strings, private keys, `KEY=value`) on every persistence/prompt/event path; `SecretStr` config; child processes get no inherited secrets | [credentials.md](docs/security/credentials.md) |
| Limits | Max time, actions, LLM calls, vision calls, retries per step, re-plans, shell operations, adaptive iterations, tabs, concurrent tasks; approval timeout | stops runaway agents |
| Audit | Every permission decision, approval (who/when/scope/note), tool call (redacted args), observation, verification result and event is persisted with correlation IDs | |

## Approvals

An approval request always states **what** the agent wants to do, **why**, **which resource** it touches and **what could go wrong**;
the technical arguments (redacted) are one click away. Scopes: *once* or *for this task* (not offered for critical risk, arbitrary code
execution, installs, credential entry, or in tainted tasks). Focus starts on **Deny**; Escape denies. See [approvals.md](docs/security/approvals.md).

## Known limitations (please read)

These are real gaps, listed so you can decide whether the residual risk is acceptable:

1. **Prompt-injection detection is heuristic.** It is deterministic and tested against many phrasings/obfuscations, but a novel attack can
   evade it. The real backstops are: provenance blocking, taint-driven approvals, the permission engine, and *you* reading approvals.
   A model that is tricked into choosing an action on a resource the page never named will reach an approval dialog flagged as tainted.
2. **Approved code runs with your privileges.** `python main.py`, `npm test`, etc. execute arbitrary code once approved. There is no
   sandbox/Job-Object/AppContainer around child processes; the controls are the approval, a scrubbed environment, a restricted cwd and
   timeouts. Do not approve code you would not run yourself.
3. **Input injection into apps that host a terminal** (e.g. VS Code's integrated terminal) cannot be blocked by process name; only
   standalone terminals (`cmd`, PowerShell, Windows Terminal, bash…) are blocked.
4. **SSRF coverage is not total.** Top-level navigations and downloads are DNS-resolved and checked; sub-resource requests are blocked
   for literal private IPs and internal names but are not DNS-resolved per request (DNS-rebinding gap).
5. **Single user, token auth, no TLS.** It is designed for loopback. Do not expose it to a network; if you must, put it behind a reverse
   proxy with TLS and additional authentication. There is no rate limiting.
6. **Data leaves the machine to the LLM provider**: your instruction, tool outputs (file text, page text, window text) and, if the
   vision fallback is used, screenshots. Redaction is pattern-based and cannot recognise arbitrary free-form secrets.
7. **Audit rows are not tamper-evident** (an administrator with database access can alter them).
8. **UI Automation reads on-screen content.** Password-like fields are masked by heuristics only.
9. **Real LLM providers were verified against recorded wire formats, not live endpoints** (no API keys in the development environment);
   run a smoke test with your key before relying on it. See [PROJECT_STATUS.md](PROJECT_STATUS.md).

## Hardening checklist for a real deployment

* Set `SAA_ENV=prod` and a long random `SAA_SECURITY__API_TOKEN`; keep `SAA_HOST=127.0.0.1`.
* Keep `SAA_TERMINAL__ALLOW_SHELL_INTERPRETERS=false`, `SAA_BROWSER__ALLOW_UPLOAD=false`, `SAA_BROWSER__ALLOW_PRIVATE_NETWORK=false`.
* Narrow `SAA_FILESYSTEM__ALLOWED_ROOTS` to the folders you actually want automated.
* Consider `SAA_SECURITY__BLOCK_ON_INJECTION=true` and setting MEDIUM risk to *ask* (Permissions page) if you work with untrusted content.
* Run under a dedicated, low-privilege Windows account when possible.
* Use a PostgreSQL password that is not the compose default.
