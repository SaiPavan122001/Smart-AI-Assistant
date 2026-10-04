# Credential and secret handling

## What is a secret here

LLM/vision API keys, the API bearer token, database password, anything the *user* types into a password field through the agent,
anything secret-shaped that appears in files, pages or command output.

## Rules

| Where | Rule | Implementation |
|---|---|---|
| Configuration | Env vars / `.env`; `SecretStr`; never in `repr`/JSON dumps | `config/settings.py` |
| Frontend | Never receives API keys. The *API token* lives in `sessionStorage` for the tab only; WebSocket auth uses a subprotocol, not the URL | `frontend/src/services` |
| Logs | JSON formatter redacts messages, `extra` fields and exceptions; access logs off | `observability/logging.py` |
| Events / tool results / task history / audit | `redact()` on every event payload, tool result, approval arguments, tool-call record | `state/events.py`, `tools/router.py` |
| LLM prompts | Outbound messages and system prompt pass through `redact_text` in the gateway | `llm/gateway.py` |
| Provider errors | Error bodies are redacted and truncated before surfacing | `llm/http.py` |
| Child processes | Environment is an allowlist; secret-looking variables are never inherited | `security/commands.py::build_environment` |
| Credential entry | Typing into a password/secret/payment-looking field is *high* risk, once-only approval, and the text is replaced by `***MASKED***` in events, approvals and records (the tool still receives the real value) | `RiskAssessment.mask_args` |
| Browser sessions | Fresh incognito-style context per task; no persistent profile; cookies never shared between tasks | `tools/browser/manager.py` |
| Desktop reads | Password-like controls report no value | `tools/desktop` |

## Redaction

*Key-based* (whole-word tokens, so `passed`/`bypass`/`author` are not masked while `password`, `api_key`, `accessToken`, `Authorization`,
`client_secret`, `session_id`, `DATABASE_URL`… are) and *value-based* (Anthropic/OpenAI/GitHub/AWS/Slack/Google keys, JWTs, `Bearer …`,
PEM private keys, `scheme://user:pass@host`, `NAME=value` assignments with secret-like names). Numeric counters (token usage) are never masked.

**Limit:** redaction is pattern-based. A free-form secret with no recognisable shape or key name (e.g. a password in prose) can still be
persisted or sent to the model if it is in a file/page the agent reads.

Tests: `unit/test_redact.py` (key/value matrix, recursion, non-mutation), `unit/test_router.py::test_secrets_are_redacted_everywhere`,
`::test_masked_arguments_never_reach_logs_approvals_or_records`, `unit/test_llm.py::test_gateway_accounting_budget_and_redaction`,
`integration/test_startup.py` (the real server never logs its token), `integration/test_browser.py::test_forms_select_and_sensitive_actions`.
