# Perception hierarchy

The agent prefers the cheapest, fastest, most reliable observation that answers the question:

| # | Source | Used for | Cost | Where |
|---|---|---|---|---|
| 1 | Direct API | downloads via HTTP (`browser_download url=…`), future API tools | lowest | `browser/session.py::download_url` |
| 2 | Structured data | file listings/stats, command results, verification checks | very low | filesystem/terminal/verification tools |
| 3 | Browser DOM | interactive elements (role, accessible name, href, state) | low | `browser/elements.py` (injected JS, shadow-DOM aware) |
| 4 | Accessibility tree | `browser_read mode=aria` (Playwright `aria_snapshot`) | low | `browser/session.py::read` |
| 5 | Windows UI Automation | window list, control tree, properties, patterns | low–medium | `desktop/uia.py` |
| 6 | Deterministic automation | UIA-addressed mouse/keyboard, PyAutoGUI for vision-confirmed coordinates | medium | `desktop/service.py`, `desktop/fallback.py` |
| 7 | Vision model | when structure is unavailable or the question is visual | high | `vision/` (explicit tools) |
| 8 | OCR | optional provider interface (`OcrProvider`) | high | `vision/service.py` |

Every `ToolResult` carries `perception_source`, and every read-only result is stored as an `Observation` (source, subject, content hash,
untrusted flag, approximate tokens), so you can see *how* the agent knew something.

## Targeted retrieval (browser)

`browser_find(query, role?)` ranks elements with a deterministic scorer (`utils/text_match.py` + `elements.py`): exact/prefix/substring name
matches, significant-token coverage (stop-words and ≤2-letter words carry no weight; stems and word-prefixes match), role agreement, enabled,
in-viewport. Scores ≥ 50 are *actionable*; 30–49 are only suggestions; ties within 5 points are **ambiguous** and reported rather than guessed.
The result is a handful of `role "name" ref=eN` lines, typically < 500 tokens for a page with hundreds of controls.

Refs are stamped as `data-saa-ref` attributes with a session-monotonic counter, so they never repeat. A ref whose element is gone is **stale**:
the session refreshes once and tries to re-find the *same logical control* (same role/tag/name[/href]); otherwise it raises `stale_reference`
(retryable) and the recovery engine takes over.

## Browser state cache

`BrowserState`: `current_url, page_title, active_tab, tab_count, known_elements, last_action, last_observation, navigation_state, generation`.
`generation` is bumped by navigation, popups, tab changes and clicks; element lists older than the generation are re-extracted before use.

## Caching and de-duplication (token/cost control)

* **Result cache**: read-only tools marked `cacheable` return the cached `ToolResult` for an identical call until any mutating tool of the same
  capability runs (terminal/filesystem mutations invalidate everything). Hits are counted (`cache_hits`) and cost no tool/LLM budget.
* **Unchanged-content suppression**: `browser_read` returns `unchanged` (with the URL/title) when the content hash equals the previous read.
* **Context compression**: adaptive loops keep the last results in full and trim older ones; tool results are summary-first.

## Desktop: handling poorly instrumented apps

`desktop_find_element`/`desktop_read` report `ui_quality: good | poor | empty`. On a miss the error includes `quality` and a `fallback` hint
(`vision_capture + vision_analyze`). Vision results contain boxes with confidence; `desktop_click(vision_ref=…)` refuses below the configured
confidence (`low_confidence`: "capture a tighter region, re-analyse, or ask the user").

## Vision accounting

`vision_capture` requires a `reason` (≥ 8 chars, recorded) and refuses protected windows; images are downscaled to ≤ 1568 px before being
sent; `vision_analyze` is metered by `max_vision_calls` and reported separately from LLM calls. Nothing captures screenshots automatically.
