"""Assemble the default tool registry."""

from __future__ import annotations

from app.tools.filesystem.tools import FILESYSTEM_TOOLS
from app.tools.registry import ToolRegistry
from app.tools.terminal.tools import TERMINAL_TOOLS


def build_registry(*, include_browser: bool = True, include_desktop: bool = True,
                   include_vision: bool = True) -> ToolRegistry:  # fmt: skip
    registry = ToolRegistry()
    for cls in [*FILESYSTEM_TOOLS, *TERMINAL_TOOLS]:
        registry.register(cls())
    if include_browser:
        from app.tools.browser.tools import BROWSER_TOOLS

        for cls in BROWSER_TOOLS:
            registry.register(cls())
    if include_desktop:
        from app.tools.desktop.tools import DESKTOP_TOOLS

        for cls in DESKTOP_TOOLS:
            registry.register(cls())
    if include_vision:
        from app.tools.vision.tools import VISION_TOOLS

        for cls in VISION_TOOLS:
            registry.register(cls())
    from app.verification.tools import VERIFICATION_TOOLS

    for cls in VERIFICATION_TOOLS:
        registry.register(cls())
    return registry
