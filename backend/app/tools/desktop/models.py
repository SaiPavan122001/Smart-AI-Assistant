from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel, Field


class Rect(BaseModel):
    left: int = 0
    top: int = 0
    right: int = 0
    bottom: int = 0

    @property
    def center(self) -> tuple[int, int]:
        return (self.left + self.right) // 2, (self.top + self.bottom) // 2

    @property
    def area(self) -> int:
        return max(0, self.right - self.left) * max(0, self.bottom - self.top)


class WindowInfo(BaseModel):
    handle: int
    title: str
    process: str = ""
    pid: int = 0
    active: bool = False
    minimized: bool = False
    rect: Rect = Field(default_factory=Rect)


class UIElement(BaseModel):
    ref: str
    role: str
    name: str = ""
    automation_id: str = ""
    class_name: str = ""
    enabled: bool = True
    focused: bool = False
    value: str | None = None
    sensitive: bool = False
    rect: Rect = Field(default_factory=Rect)
    depth: int = 0

    def brief(self) -> str:
        bits = [f'{self.role} "{self.name[:70]}"', f"ref={self.ref}"]
        if self.automation_id:
            bits.append(f"id={self.automation_id}")
        if not self.enabled:
            bits.append("disabled")
        return " ".join(bits)


class WindowSel(BaseModel):
    title: str | None = Field(
        default=None, description="Substring of the window title (case-insensitive)"
    )
    process: str | None = Field(default=None, description="Process name, e.g. 'Code.exe'")
    handle: int | None = None


class Snapshot(BaseModel):
    window: WindowInfo
    elements: list[UIElement]
    truncated: bool = False
    quality: str = "good"  # good | poor | empty  (how much structure the app exposes)


class ClickOutcome(BaseModel):
    method: str  # invoke | toggle | select | mouse | point
    element: UIElement | None = None


class DesktopError(Exception):
    """Backend-level failure with a machine-readable kind."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind  # not_found | stale | unsupported | failed | unavailable
        self.message = message


class DesktopBackend(Protocol):
    """Accessibility-driven desktop access. Implementations: UIA (Windows), in-memory (tests)."""

    name: str

    def available(self) -> bool: ...
    def list_windows(self) -> list[WindowInfo]: ...
    def active_window(self) -> WindowInfo | None: ...
    def snapshot(self, win: WindowSel, *, max_depth: int, max_elements: int) -> Snapshot: ...
    def element(self, ref: str) -> UIElement | None: ...
    def click(self, ref: str) -> ClickOutcome: ...
    def type_text(self, ref: str, text: str, *, clear: bool) -> str: ...
    def read_element(self, ref: str) -> dict[str, Any]: ...
    def read_text(self, win: WindowSel, *, max_chars: int) -> str: ...
    def scroll(self, ref: str | None, win: WindowSel, direction: str, amount: int) -> str: ...
    def focus(self, win: WindowSel) -> WindowInfo: ...
    def send_keys(self, keys: list[str]) -> None: ...
    def launch(self, executable: str, args: list[str]) -> int: ...
