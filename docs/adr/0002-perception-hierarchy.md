# ADR-0002: Structured perception first; vision is an explicit, metered fallback

**Context.** Screenshot→OCR→vision→click loops are slow, expensive, flaky and the least secure channel (pixels can contain instructions).

**Decision.** Perception priority: API → structured data → DOM → accessibility tree → Windows UI Automation → deterministic automation → vision → OCR.
Vision is exposed only as two tools (`vision_capture` with a recorded reason, `vision_analyze`), metered by `max_vision_calls`, returning structured elements
with confidence; coordinate clicks require a vision reference above the confidence threshold. No component captures screenshots implicitly.

**Consequences.** + Cheap, deterministic, testable perception; tiny contexts (targeted `find`). − Apps with no accessibility tree (some Electron/legacy apps) need the
vision path, which depends on a configured provider and is less reliable; the system reports `ui_quality` and degrades honestly.
