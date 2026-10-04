# ADR-0005: External content is data; deterministic detection + taint + provenance

**Context.** Pages, documents and command output can carry instructions aimed at the agent. A purely LLM-based classifier is itself injectable and non-deterministic.

**Decision.** (1) Tools mark outputs from external sources as untrusted; (2) outputs are scanned by deterministic rules after Unicode normalisation; (3) delivered to the model inside
nonce-fenced `<external_data>` blocks; (4) a per-task *taint* escalates approvals and disables trusted operations/task grants; (5) mutating calls whose arguments reuse flagged text — or a resource
named near it that the user did not name — are blocked before any approval; (6) permissions, policy and tool availability live in code/config the content cannot reach.

**Consequences.** + Testable, reproducible, cheap; stops the "delete the file the page mentioned" class outright. − Heuristic: unknown phrasings can evade detection; residual risk is handled by approvals
and documented. − Some false positives (e.g. a security blog post) taint a task and cause extra approvals — a deliberate bias toward safety.
