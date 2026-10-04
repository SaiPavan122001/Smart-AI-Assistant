# Permission architecture

```
LLM ─▶ Tool Router ─▶ Permission Engine ─▶ Tool ─▶ Computer
```

* **Policy** (`permissions/policy.py::PermissionPolicy`, persisted in the `permissions` table, editable on the Permissions page / `PUT /api/v1/permissions`):
  `risk_actions` (default `low=allow, medium=allow, high=ask, critical=ask`), `tool_overrides`, `category_overrides`, `trusted_operations`
  (`tool + resource glob + max risk`), `disabled_capabilities`. `normalized()` guarantees CRITICAL can never be auto-allowed. Updating bumps `version`.
* **Engine** (`permissions/engine.py`): pure `decide(...)`; ordering and semantics are in [approvals.md](../security/approvals.md).
* **Approvals** (`permissions/approvals.py`): create → wait (cancellable, with timeout) → resolve; scope downgrade; cancel-by-task.
* **Where policy is also enforced**: tool `assess()` (hard denies from `PathPolicy`/command/URL/window/key policies) and again inside `run()`.

## Risk classes in practice

| Risk | Examples | Default |
|---|---|---|
| low | read/list/search files, read/find on a page, open the browser, navigate, read-only queries, desktop reads, focus/scroll | allow |
| medium | create/move/copy files, downloads, type into non-sensitive fields, launch an allowlisted app, `git add/commit`, static checks, screen capture/vision analysis | allow (audited) |
| high | delete, overwrite, run code (`python x.py`, `npm test`), install packages, `git push/reset`, purchase-like clicks, credential entry, risky key combos (Alt+F4) | ask |
| critical | recursive folder delete, shell interpreter (if ever enabled), anything the tool marks critical | always ask; never scoped; never trusted |

## Taint interaction

When the task has read injection-flagged content, MEDIUM+ actions that policy would allow become *ask*, trusted operations and task grants are
ignored, and scopes are limited to *once* ([prompt-injection.md](../security/prompt-injection.md)).

## Audit trail

Every decision is stored (`permissions` rows: tool call, tool, risk, action, reason, via) and every approval (`approvals` rows). `GET /tasks/{id}/permission-decisions`
and `/approvals` expose them.
