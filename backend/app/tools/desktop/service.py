"""Desktop service: policy guards + fallback hierarchy on top of a DesktopBackend.

Fallback hierarchy (cheapest/most reliable first):
  1. Windows UI Automation patterns (invoke / value / select / toggle)
  2. UIA-addressed mouse/keyboard input (deterministic, element-relative)
  3. Coordinate input from a *confident* vision result (PyAutoGUI)  [explicitly requested]
  4. Vision / OCR perception (see app.tools.vision)

All backend calls run on one dedicated COM worker thread.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, TypeVar, cast

from app.config.settings import DesktopSettings
from app.tools.desktop.fallback import NoFallback, PyAutoGuiFallback
from app.tools.desktop.models import (
    ClickOutcome,
    DesktopBackend,
    DesktopError,
    Snapshot,
    UIElement,
    WindowInfo,
    WindowSel,
)
from app.utils.errors import AgentError, ErrorCode, PolicyViolation
from app.utils.text_match import ACT_THRESHOLD, SUGGEST_THRESHOLD, partial_score

T = TypeVar("T")
_WORD = re.compile(r"[a-z0-9]+")
_ROLE_ALIASES = {
    "input": "textbox", "edit": "textbox", "text field": "textbox", "field": "textbox", "btn": "button",
    "dropdown": "combobox", "select": "combobox", "a": "link", "menu item": "menuitem", "editor": "document",
}  # fmt: skip


# Launching these with no arguments legitimately opens a NEW window (and the shell owns many windows).
_ALWAYS_NEW_WINDOW = frozenset({"explorer"})


def _same_app(exe: str, process: str) -> bool:
    """'calc.exe' ~ 'CalculatorApp.exe', 'notepad.exe' ~ 'Notepad.exe', 'code' ~ 'Code.exe'."""
    a, b = Path(exe).stem.lower(), Path(process).stem.lower()
    return bool(a and b) and (a == b or b.startswith(a) or a.startswith(b))


def _tokens(s: str) -> list[str]:
    return _WORD.findall(s.lower())


# How UI Automation *names* keys/symbols: the query "2" or "*" must find the control "Two" / "Multiply by".
_SYMBOL_NAMES: dict[str, tuple[str, ...]] = {
    "0": ("zero",), "1": ("one",), "2": ("two",), "3": ("three",), "4": ("four",), "5": ("five",),
    "6": ("six",), "7": ("seven",), "8": ("eight",), "9": ("nine",),
    "+": ("plus", "add"), "-": ("minus", "subtract"), "−": ("minus", "subtract"),
    "*": ("multiply by", "multiply", "times"), "x": ("multiply by",), "×": ("multiply by", "times"),
    "/": ("divide by", "divide"), "÷": ("divide by", "divide"), "=": ("equals", "equal"),
    ".": ("decimal separator", "point"), "%": ("percent",),
}  # fmt: skip


def _query_variants(q: str) -> list[str]:
    """The query itself plus, for a bare symbol, the spoken names UI Automation gives that control."""
    return [q, *_SYMBOL_NAMES.get(q, ())]


def _name_score(q: str, name: str) -> float:
    if name == q:
        return 100.0
    if name.startswith(q) and len(q) >= 3:
        return 85.0
    if (
        len(q) < 3
    ):  # 1-2 characters match only whole words, never inside other words ('x' in 'Maximize')
        return 70.0 if q in _tokens(name) else 0.0
    if q in name:
        return 70.0
    return partial_score(q, name)


# Generic words describing a text-entry control rather than naming one ("text area", "the editor", "input box").
_TEXT_ENTRY_WORDS = frozenset({
    "text", "area", "box", "field", "input", "editor", "edit", "editing", "textarea", "textbox", "document",
    "main", "content", "body", "entry", "the", "a", "an", "of", "for", "in", "typing", "write", "writing",
})  # fmt: skip
_TEXT_ENTRY_ROLES = frozenset({"document", "textbox"})


def is_text_entry_descriptor(query: str, window_title: str = "") -> bool:
    """True when the query only *describes* a text-entry control ("Notepad text area").

    Words that name the window itself (they appear in its title) are dropped first, so the model
    mentioning the app does not turn a generic descriptor into an unmatchable name.
    """
    title_words = set(_tokens(window_title))
    words = [w for w in _tokens(query) if w not in title_words]
    return bool(words) and all(w in _TEXT_ENTRY_WORDS for w in words)


def score_ui(
    elements: list[UIElement],
    query: str | None,
    role: str | None,
    automation_id: str | None,
    window_title: str = "",
    *,
    _descriptors: bool = True,
) -> list[tuple[float, UIElement]]:
    q = (query or "").lower().strip()
    if _descriptors and q and not automation_id and is_text_entry_descriptor(q, window_title):
        by_role = _score_text_entry(elements, role)
        named = score_ui(elements, query, role, automation_id, window_title, _descriptors=False)
        best: dict[str, tuple[float, UIElement]] = {}
        for s, el in [*by_role, *named]:  # a real name match always beats the generic role match
            if el.ref not in best or s > best[el.ref][0]:
                best[el.ref] = (s, el)
        return sorted(best.values(), key=lambda t: -t[0])
    want = _ROLE_ALIASES.get((role or "").lower(), (role or "").lower() or None)
    out: list[tuple[float, UIElement]] = []
    for el in elements:
        s = 0.0
        if automation_id:
            if el.automation_id.lower() == automation_id.lower():
                s = 120.0
            elif not q:
                continue
        name = el.name.lower()
        if q:
            for cand in _query_variants(q):
                s = max(s, _name_score(cand, name))
        if s <= 0:
            continue
        if want:
            s += 10.0 if el.role == want else -15.0
        if not el.enabled:
            s -= 8.0
        if s < SUGGEST_THRESHOLD:
            continue
        out.append((s, el))
    out.sort(key=lambda t: -t[0])
    return out


def _score_text_entry(elements: list[UIElement], role: str | None) -> list[tuple[float, UIElement]]:
    """Role-based match for generic text-entry descriptors. Several candidates stay *ambiguous* (never guessed)."""
    want = _ROLE_ALIASES.get((role or "").lower(), (role or "").lower()) or None
    roles = _TEXT_ENTRY_ROLES
    if want in _TEXT_ENTRY_ROLES and any(el.role == want for el in elements):
        roles = frozenset(
            {want}
        )  # the requested role exists: honour it (apps differ: Notepad exposes 'document')
    out = [(60.0 - (0 if el.enabled else 8.0), el) for el in elements if el.role in roles]
    out.sort(key=lambda t: -t[0])
    return out


class DesktopService:
    def __init__(
        self,
        backend: DesktopBackend,
        settings: DesktopSettings,
        *,
        fallback: PyAutoGuiFallback | NoFallback | None = None,
    ) -> None:
        self.backend = backend
        self.settings = settings
        self.fallback = (
            fallback
            if fallback is not None
            else (PyAutoGuiFallback() if settings.allow_coordinate_fallback else NoFallback())
        )
        # One worker thread: pywinauto/comtypes require consistent COM apartment per thread.
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="saa-desktop")
        self._known: dict[str, UIElement] = {}
        self._ref_window: dict[str, int] = {}
        self._snap_cache: dict[int, tuple[float, Snapshot]] = {}
        self._lock = (
            asyncio.Lock()
        )  # serialise desktop operations: there is one mouse and one keyboard

    async def _run(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(self._pool, lambda: fn(*args, **kwargs))
        except DesktopError as exc:
            raise _to_agent_error(exc) from exc

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)

    def available(self) -> bool:
        try:
            return bool(self.backend.available())
        except Exception:
            return False

    # ------------------------------------------------------------------ guards
    def blocked_reason(self, win: WindowInfo, *, for_input: bool) -> str | None:
        title = win.title.lower()
        for pat in self.settings.blocked_window_patterns:
            if pat.lower() in title:
                return f"Window '{win.title}' is protected (matches '{pat}')"
        if for_input and win.process.lower() in {
            p.lower() for p in self.settings.blocked_input_processes
        }:
            return (
                f"Sending input to '{win.process}' is blocked: terminals must be used through the "
                "controlled terminal tools, which classify and validate commands"
            )
        return None

    async def _guard_window(self, sel: WindowSel, *, for_input: bool) -> WindowInfo:
        win = await self.resolve_window(sel)
        reason = self.blocked_reason(win, for_input=for_input)
        if reason:
            raise PolicyViolation(reason)
        return win

    # ------------------------------------------------------------------ perception
    async def windows(self) -> list[WindowInfo]:
        return list(await self._run(self.backend.list_windows))

    async def active(self) -> WindowInfo | None:
        return await self._run(self.backend.active_window)  # type: ignore[no-any-return]

    async def resolve_window(self, sel: WindowSel) -> WindowInfo:
        wins = await self.windows()
        cands = wins
        if sel.handle is not None:
            cands = [w for w in wins if w.handle == sel.handle]
        else:
            if sel.process:
                p = sel.process.lower()
                cands = [w for w in cands if w.process.lower() in (p, p + ".exe")]
            if sel.title:
                t = sel.title.lower()
                cands = [w for w in cands if w.title.lower() == t] or [
                    w for w in cands if t in w.title.lower()
                ]
            if not sel.process and not sel.title:
                cands = [w for w in wins if w.active]
        if not cands:
            raise AgentError(
                f"No window matches {sel.model_dump(exclude_none=True) or 'the active window'}. "
                "Use desktop_windows to list open windows.",
                code=ErrorCode.ELEMENT_NOT_FOUND,
            )
        cands.sort(key=lambda w: (not w.active, w.minimized))
        return cands[0]

    async def inspect(
        self, sel: WindowSel, *, max_elements: int | None = None, fresh: bool = False
    ) -> Snapshot:
        win = await self._guard_window(sel, for_input=False)
        cap = max_elements or self.settings.max_tree_elements
        cached = self._snap_cache.get(win.handle)
        if (
            cached
            and not fresh
            and (time.monotonic() - cached[0]) < 1.5
            and len(cached[1].elements) >= min(cap, 1)
        ):
            return cached[1]
        snap: Snapshot = await self._run(
            self.backend.snapshot,
            WindowSel(handle=win.handle),
            max_depth=self.settings.max_tree_depth,
            max_elements=cap,
        )
        self._snap_cache[win.handle] = (time.monotonic(), snap)
        for el in snap.elements:
            self._known[el.ref] = el
            self._ref_window[el.ref] = win.handle
        return snap

    def invalidate(self) -> None:
        self._snap_cache.clear()

    async def find(
        self,
        sel: WindowSel,
        *,
        query: str | None,
        role: str | None,
        automation_id: str | None,
        limit: int = 5,
    ) -> tuple[Snapshot, list[tuple[float, UIElement]], bool]:
        snap = await self.inspect(sel, max_elements=max(self.settings.max_tree_elements, 250))
        ranked = score_ui(snap.elements, query, role, automation_id, snap.window.title)
        if not ranked:
            snap = await self.inspect(
                sel, max_elements=max(self.settings.max_tree_elements, 250), fresh=True
            )
            ranked = score_ui(snap.elements, query, role, automation_id, snap.window.title)
        ambiguous = len(ranked) > 1 and ranked[0][0] >= 50 and ranked[0][0] - ranked[1][0] < 5
        return snap, ranked[:limit], ambiguous

    async def resolve_element(
        self,
        *,
        ref: str | None,
        sel: WindowSel,
        query: str | None,
        role: str | None,
        automation_id: str | None,
    ) -> UIElement:
        if ref:
            known = self._known.get(ref)
            if known is not None and await self._run(self.backend.element, ref) is not None:
                return known
            # stale: re-inspect the same window and look for the same logical control
            handle = self._ref_window.get(ref)
            if known is not None and handle is not None:
                snap = await self.inspect(WindowSel(handle=handle), max_elements=300, fresh=True)
                twins = [e for e in snap.elements if e.role == known.role and e.name == known.name
                         and (not known.automation_id or e.automation_id == known.automation_id)]  # fmt: skip
                if len(twins) == 1:
                    return twins[0]
            raise AgentError(
                f"Element '{ref}' is stale or unknown. Call desktop_find_element again for fresh refs.",
                code=ErrorCode.STALE_REFERENCE, retryable=True,
            )  # fmt: skip
        if not (query or automation_id):
            raise AgentError(
                "Provide 'ref', 'query' or 'automation_id'", code=ErrorCode.INVALID_INPUT
            )
        snap, ranked, ambiguous = await self.find(
            sel, query=query, role=role, automation_id=automation_id, limit=4
        )
        ranked = [(s, e) for s, e in ranked if s >= ACT_THRESHOLD]
        if not ranked:
            raise AgentError(
                f"No UI element matches '{query or automation_id}' in '{snap.window.title}'. "
                f"UI Automation exposure for this app is '{snap.quality}'.",
                code=ErrorCode.ELEMENT_NOT_FOUND,
                details={"quality": snap.quality, "fallback": "vision_capture + vision_analyze" if snap.quality != "good" else "browse the tree with desktop_read(mode='tree')"},
            )  # fmt: skip
        if ambiguous:
            raise AgentError(
                f"'{query}' matches several elements equally well; choose by ref or automation_id.",
                code=ErrorCode.AMBIGUOUS_TARGET,
                details={"candidates": [{"ref": e.ref, "role": e.role, "name": e.name, "automation_id": e.automation_id} for _, e in ranked]},
            )  # fmt: skip
        return ranked[0][1]

    # ------------------------------------------------------------------ actions
    async def click(self, el: UIElement) -> ClickOutcome:
        win = await self._window_of(el)
        if win:
            await self._guard_window(WindowSel(handle=win), for_input=True)
        async with self._lock:
            out: ClickOutcome = await self._run(self.backend.click, el.ref)
        self.invalidate()
        return out

    async def click_point(self, x: int, y: int, *, sel: WindowSel) -> None:
        win = await self._guard_window(sel, for_input=True)
        r = win.rect
        if not (r.left <= x <= r.right and r.top <= y <= r.bottom):
            raise AgentError(
                "Click point is outside the target window", code=ErrorCode.INVALID_INPUT
            )
        async with self._lock:
            await self._run(self.fallback.click_point, x, y)
        self.invalidate()

    async def type_text(self, el: UIElement, text: str, *, clear: bool) -> str:
        win = await self._window_of(el)
        if win:
            await self._guard_window(WindowSel(handle=win), for_input=True)
        async with self._lock:
            method: str = await self._run(self.backend.type_text, el.ref, text, clear=clear)
        self.invalidate()
        return method

    async def send_keys(self, keys: list[str]) -> WindowInfo | None:
        active = await self.active()
        if active is not None:
            reason = self.blocked_reason(active, for_input=True)
            if reason:
                raise PolicyViolation(reason)
        async with self._lock:
            await self._run(self.backend.send_keys, keys)
        self.invalidate()
        return active

    async def focus(self, sel: WindowSel) -> WindowInfo:
        win = await self._guard_window(sel, for_input=False)
        async with self._lock:
            out: WindowInfo = await self._run(self.backend.focus, WindowSel(handle=win.handle))
        self.invalidate()
        return out

    async def read_element(self, el: UIElement) -> dict[str, Any]:
        return cast(dict[str, Any], await self._run(self.backend.read_element, el.ref))

    async def read_text(self, sel: WindowSel, max_chars: int) -> str:
        win = await self._guard_window(sel, for_input=False)
        return cast(
            str,
            await self._run(
                self.backend.read_text, WindowSel(handle=win.handle), max_chars=max_chars
            ),
        )

    async def scroll(
        self, el: UIElement | None, sel: WindowSel, direction: str, amount: int
    ) -> str:
        win = await self._guard_window(sel, for_input=True)
        async with self._lock:
            return cast(
                str,
                await self._run(
                    self.backend.scroll,
                    el.ref if el else None,
                    WindowSel(handle=win.handle),
                    direction,
                    amount,
                ),
            )

    async def launch(
        self, app: str, args: list[str], *, new_instance: bool = False
    ) -> dict[str, Any]:
        exe_name = self.settings.apps.get(app.lower())
        if not exe_name:
            raise PolicyViolation(
                f"'{app}' is not a launchable application. Allowed: {', '.join(sorted(self.settings.apps))}"
            )
        exe = shutil.which(exe_name) or exe_name
        open_now = await self.windows()
        before = {w.handle for w in open_now}
        if not new_instance and not args and Path(exe).stem.lower() not in _ALWAYS_NEW_WINDOW:
            # State check first: an app that is already open is reused, never relaunched.
            running = [w for w in open_now if _same_app(exe, w.process)]
            if running:
                existing = next((w for w in running if w.active), running[0])
                with contextlib.suppress(Exception):  # bringing it forward is best effort
                    await self._run(self.backend.focus, WindowSel(handle=existing.handle))
                return {"pid": existing.pid, "window": existing.model_dump(), "reused": True}
        pid: int = await self._run(self.backend.launch, exe, args)
        self.invalidate()
        win: WindowInfo | None = None
        for _ in range(20):
            await asyncio.sleep(0.25)
            new = [w for w in await self.windows() if w.handle not in before]
            if new:
                win = new[0]
                break
        return {"pid": pid, "window": win.model_dump() if win else None}

    # ------------------------------------------------------------------ helpers
    async def _window_of(self, el: UIElement) -> int | None:
        return self._ref_window.get(el.ref)


def _to_agent_error(exc: DesktopError) -> AgentError:
    code = {
        "not_found": ErrorCode.ELEMENT_NOT_FOUND,
        "stale": ErrorCode.STALE_REFERENCE,
        "unsupported": ErrorCode.NOT_AVAILABLE,
        "unavailable": ErrorCode.NOT_AVAILABLE,
        "failed": ErrorCode.ELEMENT_NOT_FOUND,
    }.get(exc.kind, ErrorCode.INTERNAL)
    return AgentError(exc.message, code=code, retryable=exc.kind in {"stale"})
