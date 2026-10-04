# Threat model

## Assets

* The user's files, accounts and logged-in sessions reachable from the machine.
* The user's attention and trust in what the agent *says* it did.
* Credentials: LLM API key, API token, database password, anything on screen/in files.
* The integrity of the audit trail.

## Actors

| Actor | Capability |
|---|---|
| **User** | Sole source of intent. Trusted, but can make mistakes (approve too quickly). |
| **Malicious content** | Web pages, PDFs/documents, file names, terminal output, window text, clipboard — attacker-controlled data the agent *must* read to do its job. |
| **Malicious website / local webpage** | Can attack the *API* from the user's browser (CSRF, DNS rebinding, cross-site WebSocket). |
| **Compromised/over-eager model** | May propose any tool call, including harmful ones. Treated as untrusted *for authorisation*. |
| **Other local processes/users** | May try the loopback API. |
| **LLM provider** | Sees what we send it. |

## Trust boundaries

```
[user] ─ UI ─ REST/WS ─ [API: token, host, origin] ─ [Runtime] ─ [Router + Permission Engine] ─ [Tools] ─ [OS / network]
                                                         ▲                  │
                                      model output (untrusted for authorisation)    tool output (untrusted DATA)
```

The permission engine and tool-level policies sit *after* the model and *before* the OS; nothing the model or the content says can move them.

## Threats and mitigations

| # | Threat | Mitigation | Tests |
|---|---|---|---|
| T1 | Prompt injection via web/file/terminal output makes the agent delete/exfiltrate/run | Fencing + scanner + taint + provenance block + approvals; hard policy independent of the model | `security/test_injection.py`, `e2e/test_scenarios.py` (web + file), evals `injection_*` |
| T2 | Model asks for a destructive/system action directly | Risk assessment → approval; hard denies; allowlists | `unit/test_router.py`, `security/test_commands.py` |
| T3 | Path traversal / symlink / junction / UNC / ADS / device names | `PathPolicy` canonicalisation + root containment, executed twice (assess + run) | `unit/test_paths.py`, `unit/test_fs_tools.py` |
| T4 | Shell injection / encoded payloads / living-off-the-land | No shell; tokenizer; deny patterns; allowlist; arg validation; batch-file arg guard | `security/test_commands.py` (86 cases) |
| T5 | Terminal bypass via desktop input | Input to terminal processes denied; Win key blocked | `unit/test_desktop.py` |
| T6 | SSRF via browser (cloud metadata, localhost services) | URL policy, per-hop redirect checks, request routing guard | `unit/test_infrastructure.py`, `integration/test_browser.py` |
| T7 | Malicious download (exe/script) | Executable extensions blocked on write; size cap; unique names (no overwrite) | `integration/test_browser.py` |
| T8 | Secret leakage to logs/DB/UI/LLM | Redaction on all sinks; masked args for credential entry; scrubbed child env | `unit/test_redact.py`, `unit/test_router.py`, `unit/test_llm.py` |
| T9 | CSRF / DNS rebinding / cross-site WebSocket against the local API | Bearer token (not cookie), Host + Origin allowlists, token in WS subprotocol | `integration/test_api.py` |
| T10 | Runaway loops / cost blowups | Budgets (LLM/vision/actions/shell/time/retries/replans), loop breaker, recovery budgets | `e2e/test_scenarios.py`, evals `limits_llm_budget` |
| T11 | Approval fatigue / vague prompts | Specific approvals, safe default focus, scoped grants only where safe, tainted warnings | UI tests |
| T12 | Agent claims false success | Deterministic verification; `verified=false` labelling; honest failure summaries | evals `verification_no_false_success` |
| T13 | Race: approval granted, then stop pressed | Kill switch + cancel re-checked after approval and before execution | `unit/test_router.py::test_cancel_between_approval_and_execution…` |
| T14 | Cross-task data/session leakage | Fresh browser context per task, per-task caches/grants/taint | `integration/test_browser.py::sessions_are_isolated` |

## Out of scope / accepted risk

See "Known limitations" in [SECURITY.md](../../SECURITY.md): heuristic injection detection, unsandboxed approved code, terminal-hosting apps,
partial SSRF for sub-resources, single-user/no-TLS, provider data exposure, non-tamper-evident audit.
