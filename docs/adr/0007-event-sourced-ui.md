# ADR-0007: Event-sourced UI over WebSocket with sequence replay

**Context.** The UI must show a live, accurate timeline, survive reconnects/reloads, and show history later.

**Decision.** The runtime emits `ExecutionEvent`s (per-task `seq`, correlation IDs, redacted data), persisted to `task_events` and fanned out in-memory. The UI is a pure reducer over events;
WebSocket clients subscribe *before* replaying `seq > after_seq`, de-duplicate by `seq`, and reconnect with backoff. REST exposes the same events for finished tasks.

**Consequences.** + No separate "current state" API to keep consistent; one source of truth for live and history views. − Event payloads are part of the API contract (documented); very chatty tasks
produce many rows (acceptable for a single-user tool; retention policy is future work).
