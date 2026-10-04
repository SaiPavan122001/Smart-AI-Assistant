# Adding a desktop adapter

Desktop access goes through `DesktopBackend` (`tools/desktop/models.py`), a `Protocol`:

```python
available() ; list_windows() ; active_window()
snapshot(win: WindowSel, *, max_depth, max_elements) -> Snapshot     # structured controls with refs
element(ref) -> UIElement | None ; click(ref) ; type_text(ref, text, *, clear) ; read_element(ref) ; read_text(win, *, max_chars)
scroll(ref | None, win, direction, amount) ; focus(win) ; send_keys(keys: list[str]) ; launch(executable, args) -> pid
```

Implementations: `UIABackend` (Windows UI Automation via pywinauto — production) and `FakeDesktopBackend` (in-memory **test adapter**; never used in production).

## Writing a new backend (e.g. AT-SPI on Linux, or an app-specific automation API)

1. Return `UIElement`s with stable-ish `ref`s valid until the next snapshot; set `sensitive=True` for password-like controls and **never return their value**.
2. Report `Snapshot.quality` honestly (`good | poor | empty`) so the agent can fall back to vision.
3. Raise `DesktopError(kind, message)` with `kind ∈ {not_found, stale, unsupported, failed, unavailable}`; the service maps them to error codes (`stale` is retryable).
4. Prefer *pattern-based* actions (invoke/value/select/toggle) over synthesised input: they work in the background and need no focus. Fall back to element-relative input only when patterns are unsupported.
5. If you must type with the keyboard, **read back and verify** what arrived (apps drop keystrokes) and retry slower before reporting success — see `UIABackend.type_text`.
6. All calls run on the service's single worker thread; keep COM/thread-affine state inside the backend.
7. Do not implement your own safety policy: window/process blocking, key grammar and terminal-input denial are enforced by `DesktopService`/the tools. Do not bypass them.

## Wiring and testing

Pass your backend to `build_container(..., desktop_backend=...)` (or construct `DesktopService(backend, settings.desktop)`). Tests: copy `tests/unit/test_desktop.py` (tool behaviour through the router) and add a read-only integration test like
`tests/integration/test_uia_windows.py`; keep anything that opens windows opt-in (`SAA_UIA_INTERACTIVE=1`).
