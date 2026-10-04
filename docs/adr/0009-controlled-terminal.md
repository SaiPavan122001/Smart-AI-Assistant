# ADR-0009: No shell; parsed, classified, allowlisted commands

**Context.** Unrestricted shell access defeats every other control; blacklists alone are bypassable.

**Decision.** Commands are tokenized (metacharacters ⇒ unclassifiable ⇒ denied), checked against deny patterns *as a backstop*, then matched to a program allowlist with per-sub-command risk, resolved to an absolute
executable (never via the cwd), argument-path-validated, and executed with `shell=False`, a scrubbed environment, timeouts, output caps and tree-kill. Unknown programs are denied or approval-only. Shell interpreters are off.

**Consequences.** + Predictable, auditable commands; the primary control is the allowlist, not pattern luck. − Useful things (pipes, redirects, arbitrary tools) are intentionally unavailable; extend by adding a classifier rule or
listing a program under `terminal.extra_allowed_programs` (approval-only).
