# Approval model

## When the agent asks

`PermissionEngine.decide(spec, assessment, grants, tainted)` returns `ALLOW | ASK | DENY` as a pure function of:

* the **assessed risk** of *this call* (not just the tool): `low`, `medium`, `high`, `critical`;
* the **policy** (user-editable): default action per risk (`low/medium → allow`, `high/critical → ask`), per-tool and per-category
  overrides, trusted operations, disabled capabilities;
* task-scoped **grants**; and the **taint** flag (injection seen in this task).

Order: hard deny → disabled capability → tool/category DENY override → `approval: always` → base action (CRITICAL is always *ask*) →
taint escalation (MEDIUM+ allowed ⇒ ask) → trusted operation / task grant (never when tainted, never for CRITICAL) → ask.

## What the user sees

Specific, never vague:

| Field | Example |
|---|---|
| The agent wants to | `Delete file C:\Users\me\Documents\old-backup.zip` |
| Why | the plan step's rationale ("You asked me to delete the old backup") |
| Affected resource | `C:\Users\me\Documents\old-backup.zip` |
| What could go wrong | "Removes the item from its current location (sent to the Recycle Bin)" · taint warning when applicable |
| Technical details | tool, category, redacted arguments |

Buttons: **Deny** (initial focus; Escape also denies), **Approve for this task** (only when offered), **Approve once**.

## Scopes

| Scope | Meaning | Not offered when |
|---|---|---|
| once | this exact call | — |
| task | later calls of the same *tool + category* in this task | critical risk · arbitrary code (`python -c`) · installs · credential entry · the task is tainted |

Grants die with the task. A scope the request did not offer is silently downgraded to *once*.

## Denial is final

* A denied call is remembered by `(tool, args)`; an identical retry (including from a re-plan or an adaptive loop) is refused immediately
  **without asking again**, with the message "the user already refused this exact action".
* Recovery never retries a denial; the step fails, dependent steps are skipped, independent steps may still run, and the final report
  says exactly what was and was not done.
* Approvals that time out (`SAA_LIMITS__APPROVAL_TIMEOUT_S`, default 10 min) count as denied; cancelling a task or pressing STOP AGENT
  cancels pending approvals.

## Audit

`permissions` table: one `decision` row per tool call (allow/ask/deny, risk, reason, `via`: policy / trusted_operation / task_grant /
hard_deny / override / injection). `approvals` table: the full request, status, scope, who/when/note. Both are visible in *Task details →
Approvals* and via the API.
