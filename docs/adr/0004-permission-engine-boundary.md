# ADR-0004: Tool router + permission engine as the only path to the computer

**Context.** LLM output must never execute OS operations directly; approvals must be un-bypassable; decisions must be auditable.

**Decision.** Everything goes `LLM → ToolRouter → PermissionEngine → Tool`. The engine is a *pure function* of (policy, tool spec, per-call risk assessment, task grants, taint).
Tools produce the per-call `RiskAssessment` (including hard denies from path/command/URL/window/key policies) and re-check policy in `run()`. The router re-checks the kill
switch, pause and limits **after** approval and **before** execution. Policy is user-editable but normalised (CRITICAL can never be auto-allowed).

**Consequences.** + A single place to audit, test (24 router tests) and reason about. + Hard denies are independent of the model. − Every new tool must implement `assess()` honestly;
the registry-level tests and review are the control (see developer guide).
