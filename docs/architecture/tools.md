# Tool architecture

The full, generated list is in [`docs/tools/catalog.md`](../tools/catalog.md). This page explains the model.

## Contract

```python
class Tool(ABC, Generic[Input]):
    name; description; capability; Input (Pydantic model)
    risk; approval (never|policy|always); timeout_s; retry (RetryPolicy); idempotent; read_only; cacheable; cache_ttl_s
    audit (AuditPolicy); exposed_to_llm; returns_external_content
    def assess(args, ctx) -> RiskAssessment        # sync or async, side-effect free; may raise PolicyViolation (⇒ hard deny)
    async def run(args, ctx) -> ToolOutput         # re-validates policy at execution time
```

`ToolSpec` (name, description, JSON-schema input/output, capability, risk, approval, timeout, retry, idempotency, audit policy …) is
generated from the class and returned by `GET /api/v1/tools`.

`RiskAssessment` is *per call*: `risk, action (human text), resource, reasons, category, hard_deny, scoped_grant_allowed, mask_args`.
Examples: `fs_delete` of a non-empty folder → `critical`; `terminal_execute_safe python main.py` → `high / code_execution`;
`browser_click "Buy now"` → `high / sensitive_ui_action`; `browser_type` into a password field → `high / credential_entry` (masked).

## Registry and router

`ToolRegistry` holds tools by unique name and renders LLM tool definitions (only `exposed_to_llm`). `ToolRouter.execute` is the **only** way to run a
tool; its pipeline is described in [ARCHITECTURE.md §4](../../ARCHITECTURE.md). Notable behaviours:

* argument validation errors come back as `invalid_input` with the offending fields (so the model can repair them);
* **bounded retries** (`RetryPolicy`) only for errors marked retryable *and* tools that are idempotent/read-only or that opted in via `retry_on`;
* results are redacted; untrusted outputs are scanned and wrapped; failures from untrusted tools (e.g. a failing command's stderr) are scanned too;
* each call is persisted (redacted args, assessment, decision, result) and emitted as events; cancelled calls are persisted as `cancelled`.

## Capabilities

| Capability | Tools |
|---|---|
| filesystem | `fs_list fs_search fs_read fs_create fs_move fs_copy fs_delete` |
| terminal | `terminal_execute_safe terminal_query terminal_environment` |
| browser | `browser_open navigate find read click type select download upload tabs back forward` |
| desktop | `desktop_windows active_window find_element read click type key focus scroll launch` |
| vision | `vision_capture vision_analyze` |
| verification | `verify_file_state verify_browser_state verify_desktop_state verify_task_result` |

Tools are only registered when their backend is available (browser enabled; Windows UIA available; a vision provider configured), so the planner is
never offered a tool that cannot work.

## What is deliberately *not* exposed

Raw shell, arbitrary Python/JS evaluation, raw mouse coordinates (only vision-confirmed), raw keystroke strings (only validated combinations),
arbitrary process launch (allowlisted applications only), permanent deletion, registry access, package installation without approval.
