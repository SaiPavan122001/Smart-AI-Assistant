# ADR-0011: Declarative verification contracts; success is observed, not assumed

**Context.** A tool returning "ok" does not mean the user's goal was met (a download can be empty, a move can copy, a click can do nothing).

**Decision.** Plans carry `verify` specs per step and `success_criteria` for the whole request, each `{kind, params}` from a registry of deterministic checkers (file exists/absent/moved/contains, directory contents, download complete and stable,
browser URL/title/text/element, window exists, UI element state, value comparisons over recorded outputs). Checks poll (`wait_s`) for eventual consistency, resolve paths through the filesystem policy, and persist evidence.
Without any machine-checkable contract, an explicitly labelled LLM judgement is used and the result is `verified=false`.

**Consequences.** + The final report cannot claim what the world does not show; failed verification triggers recovery/re-planning. − Planners must express criteria; the prompt and validator push them to; tasks that are inherently subjective stay "not verified".
