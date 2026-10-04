# Adding perception providers

Perception providers turn the world into *structured* observations. Add one when a new source is cheaper/more reliable than vision for some surface (an app's COM/automation API, a REST API, a database, an accessibility bridge).

## As a tool (usual case)

Expose the provider through `read_only` tools (see [adding-tools.md](adding-tools.md)): small typed inputs, compact `data`, a `summary`, `perception_source` set to the matching
`PerceptionSource` (`api`, `structured`, `dom`, `accessibility`, `uia`, `deterministic`, `vision`, `ocr`), `returns_external_content=True` when the content is attacker-controllable,
`cacheable=True` where identical calls are pointless. The router records an `Observation` for every successful read-only call.

## Vision / OCR providers

* `VisionProvider.analyze(png, ObservationRequest, *, meter, token) -> VisionResult` (`tools/vision/service.py`). Return structured elements `{id, label, kind, bbox[x1,y1,x2,y2] in image px, confidence}` and an honest overall `confidence`.
  Charge usage through the gateway with `purpose="vision"` so it counts against `max_vision_calls`. `LLMVisionProvider` is the reference implementation.
* `OcrProvider.read(png) -> str` is used for `mode=read_text` when no vision provider is configured (always marked `needs_confirmation`).
* Register by passing your provider to `VisionService(...)` in `container.build_container`.

## Rules

* Never block the event loop; run blocking SDKs in a thread.
* Never perform actions in a perception tool.
* Respect the kill switch/cancel (`await ctx.token.run(...)` for long calls).
* Do not return secrets; password-like fields must report no value.
