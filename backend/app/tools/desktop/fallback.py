"""Deterministic coordinate-level input (PyAutoGUI). Used ONLY when UI Automation cannot
address the target, and for coordinates that come from a confident vision result.

PyAutoGUI's FAILSAFE stays enabled: slamming the mouse into a screen corner aborts any
in-flight automation, giving the human a hardware-level emergency stop.
"""

from __future__ import annotations

from typing import Any

from app.tools.desktop.models import DesktopError


class PyAutoGuiFallback:
    name = "pyautogui"

    def _pag(self) -> Any:
        try:
            import pyautogui
        except Exception as exc:  # pragma: no cover - environment dependent
            raise DesktopError(
                "unavailable", f"PyAutoGUI is unavailable: {type(exc).__name__}"
            ) from exc
        pyautogui.FAILSAFE = True
        pyautogui.PAUSE = 0.05
        return pyautogui

    def click_point(self, x: int, y: int) -> None:
        pag = self._pag()
        w, h = pag.size()
        if not (0 <= x < w and 0 <= y < h):
            raise DesktopError("failed", f"Point ({x},{y}) is outside the screen")
        pag.click(x, y)

    def hotkey(self, keys: list[str]) -> None:
        self._pag().hotkey(*keys)

    def screenshot_region(self, left: int, top: int, width: int, height: int) -> bytes:
        import io

        from PIL import ImageGrab

        img = ImageGrab.grab(bbox=(left, top, left + width, top + height), all_screens=True)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()


class NoFallback:
    """Used when coordinate fallback is disabled or unavailable (tests)."""

    name = "none"

    def click_point(self, x: int, y: int) -> None:
        raise DesktopError("unavailable", "Coordinate fallback is disabled")

    def hotkey(self, keys: list[str]) -> None:
        raise DesktopError("unavailable", "Coordinate fallback is disabled")

    def screenshot_region(self, left: int, top: int, width: int, height: int) -> bytes:
        raise DesktopError("unavailable", "Screen capture is disabled")
