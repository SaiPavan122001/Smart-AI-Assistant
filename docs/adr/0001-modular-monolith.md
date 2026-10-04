# ADR-0001: Modular monolith, no extra infrastructure

**Context.** The product is a single-user local agent that must start with one command, share in-memory state (kill switch, approvals,
per-task caches) with the request path, and be debuggable. The brief forbids Redis/Kafka/Kubernetes/microservices "for complexity".

**Decision.** One FastAPI process with strong internal module boundaries (`models` ← `utils`; tools/permissions/security/planner/orchestration as separate
packages; a single composition root). PostgreSQL is the only external service. Interfaces (`LLMProvider`, `Repository`, `DesktopBackend`,
`VisionProvider`, `Tool`) invert dependencies where a second implementation is plausible or needed for tests.

**Consequences.** + Trivial deployment, atomic kill switch, cheap in-process events. − Scaling out would need a queue and a shared event bus
(an `EventBus` port is the seam). − A bug in one module can still crash the process (mitigated by per-task exception containment).
