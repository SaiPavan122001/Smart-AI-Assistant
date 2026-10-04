# ADR-0006: Plans of deterministic steps with bounded adaptive steps

**Context.** Calling the LLM per click/keystroke is slow, costly and non-deterministic. Many tasks are predictable once the plan exists.

**Decision.** The planner emits a structured plan. `tool` steps carry fixed tool+args and run with **no LLM call**; arguments may reference earlier outputs via `{{steps.sN…}}` resolved by code.
`adaptive` steps (bounded LLM tool loop) are used only when the next action depends on unknown observations. Plans are validated deterministically (and repaired once) before execution.
Failures are handled by a code-owned recovery table; the LLM is involved only through bounded adaptive fallbacks and re-planning.

**Consequences.** + A "search, download, create folder, move" task costs one LLM call; behaviour is reproducible and evaluable with a scripted model. − Plans can be wrong about the world;
that is what verification, recovery and re-planning are for. − The planner must produce valid JSON against a fairly rich schema (validated + repaired).
