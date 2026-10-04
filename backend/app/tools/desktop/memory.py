"""In-memory desktop backend: a *test adapter*, never used in production.

It simulates windows and UI trees so the desktop tools, permission rules and agent
logic can be exercised deterministically on any OS. The production backend is
``UIABackend``. Nothing here pretends to be real UI Automation.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.tools.desktop.models import (
    ClickOutcome,
    DesktopError,
    Rect,
    Snapshot,
    UIElement,
    WindowInfo,
    WindowSel,
)


@dataclass
class FakeElement:
    role: str
    name: str = ""
    automation_id: str = ""
    value: str | None = None
    enabled: bool = True
    sensitive: bool = False
    on_click: Callable[[FakeDesktopBackend, FakeElement], None] | None = None
    rect: Rect = field(default_factory=lambda: Rect(left=10, top=10, right=110, bottom=40))


@dataclass
class FakeWindow:
    handle: int
    title: str
    process: str
    elements: list[FakeElement] = field(default_factory=list)
    minimized: bool = False


class FakeDesktopBackend:
    name = "in-memory-test-adapter"

    def __init__(self) -> None:
        self.windows: list[FakeWindow] = []
        self._active: int | None = None
        self._refs: dict[str, FakeElement] = {}
        self._seq = itertools.count(1)
        self.keys_sent: list[list[str]] = []
        self.launched: list[tuple[str, list[str]]] = []
        self.launch_handlers: dict[str, Callable[[FakeDesktopBackend], FakeWindow]] = {}
        self.typed: list[tuple[str, str]] = []
        self.clicks: list[str] = []

    # --- test setup helpers
    def add_window(
        self,
        title: str,
        process: str,
        elements: list[FakeElement] | None = None,
        *,
        active: bool = False,
    ) -> FakeWindow:
        w = FakeWindow(
            handle=1000 + len(self.windows), title=title, process=process, elements=elements or []
        )
        self.windows.append(w)
        if active or self._active is None:
            self._active = w.handle
        return w

    # --- protocol
    def available(self) -> bool:
        return True

    def _info(self, w: FakeWindow) -> WindowInfo:
        return WindowInfo(
            handle=w.handle,
            title=w.title,
            process=w.process,
            pid=w.handle,
            active=w.handle == self._active,
            minimized=w.minimized,
            rect=Rect(left=0, top=0, right=800, bottom=600),
        )

    def list_windows(self) -> list[WindowInfo]:
        return [self._info(w) for w in self.windows]

    def active_window(self) -> WindowInfo | None:
        return next((self._info(w) for w in self.windows if w.handle == self._active), None)

    def _select(self, sel: WindowSel) -> FakeWindow:
        cands = self.windows
        if sel.handle is not None:
            cands = [w for w in cands if w.handle == sel.handle]
        else:
            if sel.process:
                cands = [w for w in cands if w.process.lower() == sel.process.lower()]
            if sel.title:
                cands = [w for w in cands if sel.title.lower() in w.title.lower()]
            if not sel.process and not sel.title:
                cands = [w for w in cands if w.handle == self._active]
        if not cands:
            raise DesktopError("not_found", "No window matches")
        return cands[0]

    def snapshot(self, win: WindowSel, *, max_depth: int, max_elements: int) -> Snapshot:
        w = self._select(win)
        out: list[UIElement] = []
        for el in w.elements[:max_elements]:
            ref = f"f{next(self._seq)}"
            self._refs[ref] = el
            out.append(self._describe(ref, el))
        quality = "empty" if not out else ("poor" if len(out) <= 3 else "good")
        return Snapshot(
            window=self._info(w),
            elements=out,
            truncated=len(w.elements) > max_elements,
            quality=quality,
        )

    @staticmethod
    def _describe(ref: str, el: FakeElement) -> UIElement:
        return UIElement(ref=ref, role=el.role, name=el.name, automation_id=el.automation_id, enabled=el.enabled,
                         value=None if el.sensitive else el.value, sensitive=el.sensitive, rect=el.rect)  # fmt: skip

    def element(self, ref: str) -> UIElement | None:
        el = self._refs.get(ref)
        return self._describe(ref, el) if el else None

    def _get(self, ref: str) -> FakeElement:
        el = self._refs.get(ref)
        if el is None:
            raise DesktopError("stale", f"Element '{ref}' is unknown or stale")
        return el

    def click(self, ref: str) -> ClickOutcome:
        el = self._get(ref)
        if not el.enabled:
            raise DesktopError("failed", "Element is disabled")
        self.clicks.append(el.name or el.automation_id)
        if el.on_click:
            el.on_click(self, el)
        return ClickOutcome(method="invoke", element=self._describe(ref, el))

    def type_text(self, ref: str, text: str, *, clear: bool) -> str:
        el = self._get(ref)
        if el.role not in {"textbox", "document", "combobox"}:
            raise DesktopError("unsupported", "Element does not accept text")
        el.value = text if clear else (el.value or "") + text
        self.typed.append((el.name, text))
        return "value_pattern"

    def read_element(self, ref: str) -> dict[str, Any]:
        el = self._get(ref)
        return {
            "name": el.name,
            "role": el.role,
            "value": None if el.sensitive else el.value,
            "enabled": el.enabled,
        }

    def read_text(self, win: WindowSel, *, max_chars: int) -> str:
        w = self._select(win)
        parts = [e.value or e.name for e in w.elements if (e.value or e.name) and not e.sensitive]
        return "\n".join(parts)[:max_chars]

    def scroll(self, ref: str | None, win: WindowSel, direction: str, amount: int) -> str:
        return "scroll_pattern"

    def focus(self, win: WindowSel) -> WindowInfo:
        w = self._select(win)
        w.minimized = False
        self._active = w.handle
        return self._info(w)

    def send_keys(self, keys: list[str]) -> None:
        self.keys_sent.append(keys)

    def launch(self, executable: str, args: list[str]) -> int:
        self.launched.append((executable, args))
        handler = self.launch_handlers.get(Path(executable).stem.lower())
        if handler:
            w = handler(self)
            self.windows.append(w)
            self._active = w.handle
        return 4242
