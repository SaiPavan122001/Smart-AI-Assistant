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

## Permissions per tool

What each tool is allowed to do at all (`backend/app/permissions/classes.py`; a test compares this table and that file with the real
registry, so a tool cannot be added without being placed). Risk and category are still decided per call by the tool's `assess`; this
table is the fixed part. *Observing* permissions are proven read-only by test.

| Permission | Tools | Changes something | Default with the standard policy |
|---|---|---|---|
| `read` | `fs_read`, `fs_read_many` | no | allowed; output is untrusted data |
| `search` | `fs_list`, `fs_search`, `fs_find` | no | allowed; never leaves the allowed folders, never follows a junction or symlink |
| `create_edit` | `fs_create` | yes | allowed (medium); replacing an existing file is high: asks |
| `move` | `fs_move`, `fs_organize` | yes | allowed (medium); replacing is high: asks. `fs_organize` never overwrites or deletes |
| `copy` | `fs_copy` | yes | allowed (medium); replacing is high: asks |
| `delete` | `fs_delete` | yes | always asks (high); a non-empty folder is critical: asks every time, once only |
| `undo` | `fs_undo` | yes | only inside a rollback the user started, never from a plan or the model; allowed (medium) and only when the item is still exactly as the agent left it ([file-transactions](../file-transactions.md)) |
| `execute` | `terminal_execute_safe` | yes | read-only invocations allowed; running code or installing asks (high); shell interpreters and deny-listed commands cannot be approved |
| `query_environment` | `terminal_query`, `terminal_environment` | no | allowed |
| `browser_read` | `browser_read`, `browser_find` | no | allowed; output is untrusted data |
| `browser_navigate` | `browser_open`, `browser_navigate`, `browser_back`, `browser_forward`, `browser_tabs` | loads pages | allowed; private and internal addresses are refused |
| `browser_interact` | `browser_click`, `browser_type`, `browser_select` | yes, possibly on a remote site | allowed (low); purchase-like or submit-like clicks and credential fields ask (high) |
| `transfer` | `browser_download`, `browser_upload` | yes | download allowed (medium), executables refused; upload is off by default and asks (high) |
| `desktop_read` | `desktop_windows`, `desktop_active_window`, `desktop_find_element`, `desktop_read` | no | allowed; protected windows are invisible |
| `desktop_input` | `desktop_click`, `desktop_type`, `desktop_key`, `desktop_focus`, `desktop_scroll` | yes | allowed (low/medium); risky keys and sensitive controls ask; terminals and protected windows cannot receive input |
| `launch_application` | `desktop_launch` | yes | allowed (medium) for the configured application list only |
| `screen_capture` | `vision_capture`, `vision_analyze` | captures the screen; sends it to the vision model | allowed (medium), metered, only when requested |
| `web_search` | `web_search` | no | allowed; results are untrusted data |
| `verify` | `verify_file_state`, `verify_browser_state`, `verify_desktop_state`, `verify_task_result` | no | allowed |

Every path to a tool goes through `ToolRouter.execute`: planned steps, adaptive steps, recovery, re-plans and a task resumed after a
restart. There is no second way to run a tool, and a tool cannot be approved past a hard deny.
