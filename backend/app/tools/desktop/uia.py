"""Windows UI Automation backend (pywinauto, ``backend="uia"``).

All methods are synchronous and MUST be called from the single COM worker thread owned
by ``DesktopService`` (pywinauto/comtypes keep COM apartment state per thread).
"""

from __future__ import annotations

import contextlib
import ctypes
import os
import subprocess
import time
from collections import deque
from typing import Any

from app.tools.desktop.keys import escape_literal, to_pywinauto
from app.tools.desktop.models import (
    ClickOutcome,
    DesktopError,
    Rect,
    Snapshot,
    UIElement,
    WindowInfo,
    WindowSel,
)

_ROLE_MAP = {
    "Button": "button", "SplitButton": "button", "Edit": "textbox", "Document": "document", "ComboBox": "combobox",
    "CheckBox": "checkbox", "RadioButton": "radio", "Menu": "menu", "MenuBar": "menu", "MenuItem": "menuitem",
    "List": "list", "ListItem": "listitem", "Tree": "tree", "TreeItem": "treeitem", "Tab": "tablist",
    "TabItem": "tab", "Hyperlink": "link", "Text": "text", "ToolBar": "toolbar", "StatusBar": "statusbar",
    "DataGrid": "grid", "DataItem": "griditem", "Slider": "slider", "ScrollBar": "scrollbar",
    "Window": "window", "Pane": "pane", "Group": "group", "Image": "image", "Table": "table",
    "TitleBar": "titlebar", "Header": "header", "HeaderItem": "headeritem", "ToolTip": "tooltip",
    "Spinner": "spinner", "ProgressBar": "progressbar",
}  # fmt: skip
_INTERESTING = {
    "button", "textbox", "document", "combobox", "checkbox", "radio", "menu", "menuitem", "list", "listitem",
    "tree", "treeitem", "tablist", "tab", "link", "slider", "grid", "griditem", "window", "dialog", "toolbar",
}  # fmt: skip
_SENSITIVE_HINT = ("password", "passwort", "secret", "pin", "cvv", "card number")


def _is_password(node: Any) -> bool:
    """UI Automation exposes IsPassword for edit controls; treat failures as 'not a password field'."""
    try:
        return bool(node.element.CurrentIsPassword)
    except Exception:
        return False


def _norm_text(t: str) -> str:
    return t.replace("\r\n", "\n").replace("\r", "\n").strip()


def _rect(r: Any) -> Rect:
    try:
        return Rect(left=int(r.left), top=int(r.top), right=int(r.right), bottom=int(r.bottom))
    except Exception:
        return Rect()


class UIABackend:
    name = "windows-uia"

    def __init__(self) -> None:
        self._refs: dict[str, Any] = {}
        self._seq = 0

    # ------------------------------------------------------------------ availability
    def available(self) -> bool:
        if os.name != "nt":
            return False
        try:
            import pywinauto  # noqa: F401
            import win32gui  # noqa: F401
        except Exception:
            return False
        return True

    # ------------------------------------------------------------------ windows
    def _process_name(self, pid: int) -> str:
        try:
            import psutil

            return str(psutil.Process(pid).name())
        except Exception:
            return ""

    @staticmethod
    def _is_cloaked(hwnd: int) -> bool:
        try:
            cloaked = ctypes.c_int(0)
            ctypes.windll.dwmapi.DwmGetWindowAttribute(
                hwnd, 14, ctypes.byref(cloaked), ctypes.sizeof(cloaked)
            )
            return bool(cloaked.value)
        except Exception:
            return False

    def list_windows(self) -> list[WindowInfo]:
        import win32gui
        import win32process

        fg = win32gui.GetForegroundWindow()
        out: list[WindowInfo] = []

        def cb(hwnd: int, _: Any) -> bool:
            try:
                if not win32gui.IsWindowVisible(hwnd):
                    return True
                title = win32gui.GetWindowText(hwnd)
                if not title.strip() or self._is_cloaked(hwnd):
                    return True
                if win32gui.GetWindow(hwnd, 4):  # GW_OWNER: skip owned popups/tool windows
                    return True
                _, pid = win32process.GetWindowThreadProcessId(hwnd)
                left, top, right, bottom = win32gui.GetWindowRect(hwnd)
                out.append(
                    WindowInfo(
                        handle=hwnd,
                        title=title,
                        process=self._process_name(pid),
                        pid=pid,
                        active=hwnd == fg,
                        minimized=bool(win32gui.IsIconic(hwnd)),
                        rect=Rect(left=left, top=top, right=right, bottom=bottom),
                    )
                )
            except Exception:  # a window vanished mid-enumeration
                pass
            return True

        win32gui.EnumWindows(cb, None)
        return out

    def active_window(self) -> WindowInfo | None:
        import win32gui

        fg = win32gui.GetForegroundWindow()
        for w in self.list_windows():
            if w.handle == fg:
                return w
        return None

    def _select(self, sel: WindowSel) -> WindowInfo:
        wins = self.list_windows()
        cands = wins
        if sel.handle is not None:
            cands = [w for w in wins if w.handle == sel.handle]
        else:
            if sel.process:
                p = sel.process.lower()
                cands = [w for w in cands if w.process.lower() in (p, p + ".exe")]
            if sel.title:
                t = sel.title.lower()
                exact = [w for w in cands if w.title.lower() == t]
                cands = exact or [w for w in cands if t in w.title.lower()]
            if not sel.process and not sel.title:
                cands = [w for w in wins if w.active]
        if not cands:
            raise DesktopError(
                "not_found",
                f"No window matches {sel.model_dump(exclude_none=True) or 'active window'}",
            )
        cands.sort(key=lambda w: (not w.active, w.minimized))
        return cands[0]

    def _wrapper(self, hwnd: int) -> Any:
        from pywinauto.controls.uiawrapper import UIAWrapper
        from pywinauto.uia_element_info import UIAElementInfo

        return UIAWrapper(UIAElementInfo(handle_or_elem=hwnd))

    # ------------------------------------------------------------------ perception
    def snapshot(self, win: WindowSel, *, max_depth: int, max_elements: int) -> Snapshot:
        info = self._select(win)
        try:
            root = self._wrapper(info.handle).element_info
        except Exception as exc:
            raise DesktopError(
                "failed", f"Cannot attach UI Automation to '{info.title}': {type(exc).__name__}"
            ) from exc
        elements: list[UIElement] = []
        visited = 0
        truncated = False
        queue: deque[tuple[Any, int]] = deque([(root, 0)])
        visit_cap = max_elements * 6
        while queue:
            node, depth = queue.popleft()
            if depth >= max_depth:
                continue
            try:
                children = node.children()
            except Exception:
                continue
            for ch in children:
                visited += 1
                if visited > visit_cap:
                    truncated = True
                    queue.clear()
                    break
                el = self._describe(ch, depth + 1, info.handle)
                if el is not None and (el.role in _INTERESTING or el.name):
                    elements.append(el)
                    if len(elements) >= max_elements:
                        truncated = True
                        queue.clear()
                        break
                queue.append((ch, depth + 1))
        quality = "empty" if not elements else ("poor" if len(elements) <= 3 else "good")
        return Snapshot(window=info, elements=elements, truncated=truncated, quality=quality)

    def _describe(
        self, node: Any, depth: int, hwnd: int, ref: str | None = None
    ) -> UIElement | None:
        try:
            ctype = node.control_type or ""
            name = (node.name or "").strip()
            role = _ROLE_MAP.get(ctype, ctype.lower() or "generic")
            if role == "window" and (depth > 1 or (node.class_name or "") == "#32770"):
                role = "dialog"  # standard dialog class or a nested top-level window
            if role in {"pane", "group", "image", "text"} and not name:
                return None
            if ref is None:
                self._seq += 1
                ref = f"u{self._seq}"
                self._refs[ref] = node
                if len(self._refs) > 2000:
                    for k in list(self._refs)[:500]:
                        self._refs.pop(k, None)
            auto_id = node.automation_id or ""
            low = f"{name} {auto_id}".lower()
            enabled = True
            with contextlib.suppress(Exception):
                enabled = bool(node.enabled)
            return UIElement(
                ref=ref,
                role=role,
                name=name[:200],
                automation_id=auto_id,
                class_name=node.class_name or "",
                enabled=enabled,
                rect=_rect(node.rectangle),
                depth=depth,
                sensitive=role in {"textbox", "document"}
                and (_is_password(node) or any(h in low for h in _SENSITIVE_HINT)),
            )
        except Exception:
            return None

    def element(self, ref: str) -> UIElement | None:
        node = self._refs.get(ref)
        return self._describe(node, 0, 0, ref) if node is not None else None

    def _wrap(self, ref: str) -> Any:
        node = self._refs.get(ref)
        if node is None:
            raise DesktopError(
                "stale", f"Element '{ref}' is unknown or stale; inspect the window again"
            )
        from pywinauto.controls.uiawrapper import UIAWrapper

        try:
            w = UIAWrapper(node)
            w.is_enabled()  # raises when the element no longer exists
            return w
        except Exception as exc:
            self._refs.pop(ref, None)
            raise DesktopError(
                "stale", f"Element '{ref}' no longer exists; inspect the window again"
            ) from exc

    # ------------------------------------------------------------------ actions
    def click(self, ref: str) -> ClickOutcome:
        w = self._wrap(ref)
        el = self.element(ref)
        for method, fn in (("invoke", w.invoke), ("toggle", w.toggle), ("select", w.select)):
            if method == "toggle" and (el is None or el.role not in {"checkbox", "button"}):
                continue
            if method == "select" and (
                el is None
                or el.role not in {"listitem", "treeitem", "tab", "menuitem", "radio", "griditem"}
            ):
                continue
            try:
                fn()
                return ClickOutcome(method=method, element=el)
            except Exception:
                continue
        try:
            with contextlib.suppress(Exception):
                w.set_focus()
            w.click_input()
            return ClickOutcome(method="mouse", element=el)
        except Exception as exc:
            raise DesktopError("failed", f"Could not click element: {type(exc).__name__}") from exc

    def type_text(self, ref: str, text: str, *, clear: bool) -> str:
        """Set text via the UIA value pattern; fall back to paced keyboard input *with read-back*.

        Some apps (e.g. Windows Notepad's RichEdit) silently drop keystrokes typed too fast, so the
        keyboard path verifies what actually landed and retries slower before reporting success.
        """
        w = self._wrap(ref)
        if clear:
            try:
                w.set_edit_text(text)
                return "value_pattern"
            except Exception:
                pass
        from pywinauto.keyboard import send_keys

        previous = self._current_value(w) if not clear else ""
        expected = (previous or "") + text if not clear else text
        for attempt, pause in enumerate((0.012, 0.04, 0.09), start=1):
            try:
                with contextlib.suppress(Exception):
                    w.set_focus()
                if clear or attempt > 1:
                    send_keys("^a{BACKSPACE}", pause=0.01)
                    if not clear:
                        send_keys(
                            escape_literal(previous or ""),
                            with_spaces=True,
                            with_newlines=True,
                            pause=pause,
                        )
                send_keys(escape_literal(text), with_spaces=True, with_newlines=True, pause=pause)
            except Exception as exc:
                raise DesktopError("failed", f"Typing failed: {type(exc).__name__}") from exc
            time.sleep(min(1.0, 0.2 + 0.01 * len(text)))  # let the target app drain its input queue
            actual = self._current_value(w)
            if actual is None:  # value not readable (custom control): nothing to verify against
                return "keyboard_unverified"
            if _norm_text(actual) == _norm_text(expected):
                return "keyboard" if attempt == 1 else f"keyboard_retry{attempt - 1}"
        raise DesktopError(
            "failed",
            "Typed text did not arrive intact (the application dropped input); nothing was reported as done",
        )

    @staticmethod
    def _current_value(w: Any) -> str | None:
        try:
            return str(w.iface_value.CurrentValue)
        except Exception:
            try:
                return str(w.window_text())
            except Exception:
                return None

    def read_element(self, ref: str) -> dict[str, Any]:
        w = self._wrap(ref)
        el = self.element(ref)
        data: dict[str, Any] = {"name": el.name if el else "", "role": el.role if el else ""}
        if el is not None and el.sensitive:  # never read back the content of password-like controls
            return {**data, "value": "(hidden: sensitive field)", "enabled": el.enabled}
        for key, fn in (
            ("value", lambda: w.iface_value.CurrentValue),
            ("text", lambda: w.window_text()),
            ("toggle_state", lambda: w.get_toggle_state()),
            ("enabled", lambda: w.is_enabled()),
            ("focused", lambda: w.has_keyboard_focus()),
            ("selected", lambda: w.is_selected()),
        ):
            with contextlib.suppress(Exception):
                data[key] = fn()
        return {k: v for k, v in data.items() if v not in (None, "")}

    def read_text(self, win: WindowSel, *, max_chars: int) -> str:
        snap = self.snapshot(win, max_depth=8, max_elements=300)
        parts: list[str] = []
        for el in snap.elements:
            if el.role in {"textbox", "document"}:
                with contextlib.suppress(Exception):
                    node = self._refs[el.ref]
                    from pywinauto.controls.uiawrapper import UIAWrapper

                    v = UIAWrapper(node).iface_value.CurrentValue
                    if v and not el.sensitive:
                        parts.append(str(v))
                        continue
            if el.name and el.role in {
                "text",
                "listitem",
                "treeitem",
                "menuitem",
                "tab",
                "button",
                "link",
            }:
                parts.append(el.name)
        text = "\n".join(parts)
        return text[:max_chars]

    def scroll(self, ref: str | None, win: WindowSel, direction: str, amount: int) -> str:
        if ref is None:
            target = self._wrapper(self._select(win).handle)
        else:
            target = self._wrap(ref)
        try:
            target.scroll(direction, "page", amount)
            return "scroll_pattern"
        except Exception:
            try:
                target.wheel_mouse_input(
                    wheel_dist=amount * (3 if direction in ("up", "left") else -3)
                )
                return "mouse_wheel"
            except Exception as exc:
                raise DesktopError("failed", f"Scroll failed: {type(exc).__name__}") from exc

    def focus(self, win: WindowSel) -> WindowInfo:
        import win32con
        import win32gui

        info = self._select(win)
        if info.minimized:
            win32gui.ShowWindow(info.handle, win32con.SW_RESTORE)
        try:
            self._wrapper(info.handle).set_focus()
        except Exception:
            with contextlib.suppress(Exception):
                win32gui.SetForegroundWindow(info.handle)
        if win32gui.GetForegroundWindow() != info.handle:
            raise DesktopError("failed", f"Windows did not bring '{info.title}' to the foreground")
        return info.model_copy(update={"active": True, "minimized": False})

    def send_keys(self, keys: list[str]) -> None:
        from pywinauto.keyboard import send_keys

        send_keys(to_pywinauto(keys), pause=0.02)

    def launch(self, executable: str, args: list[str]) -> int:
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        proc = subprocess.Popen(
            [executable, *args], close_fds=True, creationflags=flags,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )  # fmt: skip
        return proc.pid
