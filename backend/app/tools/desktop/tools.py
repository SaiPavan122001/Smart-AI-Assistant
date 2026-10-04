"""Windows desktop tools. UI Automation first; vision-gated coordinates only as a fallback."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, model_validator

from app.models.enums import Capability, RiskLevel
from app.models.tool import RetryPolicy, RiskAssessment
from app.tools.base import Tool, ToolOutput
from app.tools.browser.elements import SENSITIVE_ACTION
from app.tools.context import ExecutionContext
from app.tools.desktop import keys as keymod
from app.tools.desktop.models import UIElement, WindowSel
from app.tools.desktop.service import DesktopService
from app.utils.errors import AgentError, ErrorCode, PolicyViolation

_RETRY_STALE = RetryPolicy(
    max_attempts=2, backoff_s=0.3, retry_on=[ErrorCode.STALE_REFERENCE.value]
)


def _svc(ctx: ExecutionContext) -> DesktopService:
    svc = ctx.services.desktop
    if svc is None or not svc.available():
        raise AgentError(
            "Windows UI Automation is not available (requires Windows with pywinauto). "
            "Use browser/filesystem/terminal tools instead.",
            code=ErrorCode.NOT_AVAILABLE,
        )
    return svc


class WinArgs(BaseModel):
    window_title: str | None = Field(
        default=None, description="Window title substring (default: the active window)"
    )
    window_process: str | None = Field(default=None, description="Process name such as 'Code.exe'")
    window_handle: int | None = None

    def sel(self) -> WindowSel:
        return WindowSel(
            title=self.window_title, process=self.window_process, handle=self.window_handle
        )


class ElementArgs(WinArgs):
    ref: str | None = Field(
        default=None, description="Element ref from desktop_find_element / desktop_read"
    )
    query: str | None = Field(
        default=None, description="Element name/description, e.g. 'Run', 'Search'"
    )
    role: str | None = Field(
        default=None, description="button | textbox | menuitem | listitem | checkbox | ..."
    )
    automation_id: str | None = Field(
        default=None, description="Stable UI Automation id (most reliable)"
    )


async def _element(ctx: ExecutionContext, a: ElementArgs) -> UIElement:
    return await _svc(ctx).resolve_element(
        ref=a.ref, sel=a.sel(), query=a.query, role=a.role, automation_id=a.automation_id
    )


def _peek(ctx: ExecutionContext, ref: str | None) -> UIElement | None:
    svc = ctx.services.desktop
    return svc._known.get(ref) if (svc and ref) else None


async def _window_policy(ctx: ExecutionContext, sel: WindowSel, *, for_input: bool) -> None:
    """Pre-flight: refuse protected windows before any approval is requested."""
    svc = ctx.services.desktop
    if svc is None or not svc.available():
        return
    try:
        win = await svc.resolve_window(sel)
    except AgentError:
        return  # window not found is reported by run()
    reason = svc.blocked_reason(win, for_input=for_input)
    if reason:
        raise PolicyViolation(reason)


# ----------------------------------------------------------------------------- windows
class NoArgs(BaseModel):
    pass


class DesktopWindows(Tool[NoArgs]):
    name = "desktop_windows"
    description = "List visible top-level windows (title, process, active/minimized)."
    capability = Capability.DESKTOP
    Input = NoArgs
    read_only = True
    cacheable = True
    cache_ttl_s = 2.0
    idempotent = True
    returns_external_content = True

    async def run(self, args: NoArgs, ctx: ExecutionContext) -> ToolOutput:
        svc = _svc(ctx)
        wins = [w for w in await svc.windows() if not svc.blocked_reason(w, for_input=False)]
        data: list[dict[str, Any]] = [
            {
                "handle": w.handle,
                "title": w.title[:120],
                "process": w.process,
                "active": w.active,
                "minimized": w.minimized,
            }
            for w in wins
        ]
        return ToolOutput(
            data={"windows": data}, summary=f"{len(data)} windows: " + "; ".join(f"{d['process']}: {d['title'][:40]}" for d in data[:8]),
            perception_source="uia",
        )  # fmt: skip


class DesktopActiveWindow(Tool[NoArgs]):
    name = "desktop_active_window"
    description = "Return the currently focused window."
    capability = Capability.DESKTOP
    Input = NoArgs
    read_only = True
    returns_external_content = True

    async def run(self, args: NoArgs, ctx: ExecutionContext) -> ToolOutput:
        svc = _svc(ctx)
        w = await svc.active()
        if w is None:
            return ToolOutput(
                data={"window": None}, summary="No active window", perception_source="uia"
            )
        if svc.blocked_reason(w, for_input=False):
            return ToolOutput(
                data={"window": {"title": "(protected window)", "process": w.process}},
                summary="Active window is protected",
                perception_source="uia",
            )
        return ToolOutput(
            data={"window": {"handle": w.handle, "title": w.title, "process": w.process}},
            summary=f"Active: {w.process} — {w.title[:80]}",
            perception_source="uia",
        )


# ----------------------------------------------------------------------------- find / read
class FindInput(WinArgs):
    query: str | None = None
    role: str | None = None
    automation_id: str | None = None
    limit: int = Field(default=6, ge=1, le=20)

    @model_validator(mode="after")
    def _one(self) -> FindInput:
        if not (self.query or self.automation_id):
            raise ValueError("provide 'query' or 'automation_id'")
        return self


class DesktopFindElement(Tool[FindInput]):
    name = "desktop_find_element"
    description = "Find UI controls in a window via Windows UI Automation (buttons, text boxes, menus, lists...). Returns refs, roles, names and automation ids."
    capability = Capability.DESKTOP
    Input = FindInput
    read_only = True
    idempotent = True
    returns_external_content = True

    def assess(self, args: FindInput, ctx: ExecutionContext) -> Any:
        return self._assess(args, ctx)

    async def _assess(self, args: FindInput, ctx: ExecutionContext) -> RiskAssessment:
        await _window_policy(ctx, args.sel(), for_input=False)
        return RiskAssessment(
            risk=RiskLevel.LOW,
            action=f"Inspect UI of {args.window_title or 'active window'}",
            resource=args.window_title or "active window",
            category="desktop_read",
        )

    async def run(self, args: FindInput, ctx: ExecutionContext) -> ToolOutput:
        svc = _svc(ctx)
        snap, ranked, ambiguous = await svc.find(
            args.sel(),
            query=args.query,
            role=args.role,
            automation_id=args.automation_id,
            limit=args.limit,
        )
        if not ranked:
            raise AgentError(
                f"No UI element matches '{args.query or args.automation_id}' in '{snap.window.title}' (UIA exposure: {snap.quality}).",
                code=ErrorCode.ELEMENT_NOT_FOUND,
                details={"quality": snap.quality, "elements_visible": len(snap.elements),
                         "fallback": "vision_capture + vision_analyze" if snap.quality != "good" else "desktop_read(mode='tree')"},
            )  # fmt: skip
        matches = [
            {
                **e.model_dump(include={"ref", "role", "name", "automation_id", "enabled"}),
                "score": round(s, 1),
            }
            for s, e in ranked
        ]
        return ToolOutput(
            data={"window": snap.window.title, "matches": matches, "ambiguous": ambiguous, "ui_quality": snap.quality},
            summary="; ".join(e.brief() for _, e in ranked) + (" (ambiguous: choose by ref)" if ambiguous else ""),
            perception_source="uia",
        )  # fmt: skip


class ReadInput(ElementArgs):
    mode: str = Field(
        default="tree",
        pattern="^(tree|element|text)$",
        description="tree: structured control tree | element: value/state of one control | text: visible text",
    )
    max_chars: int = Field(default=6000, ge=200, le=40000)
    max_elements: int = Field(default=80, ge=5, le=250)
    role_filter: list[str] = Field(
        default_factory=list, description="mode=tree: only these roles, e.g. ['button','textbox']"
    )


class DesktopRead(Tool[ReadInput]):
    name = "desktop_read"
    description = "Read desktop state structurally: the control tree of a window, one element's value/state, or visible text."
    capability = Capability.DESKTOP
    Input = ReadInput
    read_only = True
    cacheable = True
    cache_ttl_s = 1.5
    idempotent = True
    returns_external_content = True
    timeout_s = 45.0

    def assess(self, args: ReadInput, ctx: ExecutionContext) -> Any:
        return self._assess(args, ctx)

    async def _assess(self, args: ReadInput, ctx: ExecutionContext) -> RiskAssessment:
        await _window_policy(ctx, args.sel(), for_input=False)
        return RiskAssessment(
            risk=RiskLevel.LOW,
            action=f"Read {args.mode} of {args.window_title or 'active window'}",
            resource=args.window_title or "active window",
            category="desktop_read",
        )

    async def run(self, args: ReadInput, ctx: ExecutionContext) -> ToolOutput:
        svc = _svc(ctx)
        if args.mode == "tree":
            snap = await svc.inspect(args.sel(), max_elements=args.max_elements)
            els = [e for e in snap.elements if not args.role_filter or e.role in args.role_filter]
            lines = [("  " * min(e.depth - 1, 4)) + e.brief() for e in els[: args.max_elements]]
            return ToolOutput(
                data={"window": snap.window.title, "process": snap.window.process, "quality": snap.quality, "truncated": snap.truncated, "tree": lines},
                summary=f"UI tree of '{snap.window.title}': {len(lines)} controls (UIA exposure: {snap.quality})",
                perception_source="uia",
            )  # fmt: skip
        if args.mode == "element":
            el = await _element(ctx, args)
            data = await svc.read_element(el)
            return ToolOutput(
                data={"element": el.brief(), **data},
                summary=f"{el.brief()} → {str(data.get('value') or data.get('text') or '')[:100]}",
                perception_source="uia",
            )
        text = await svc.read_text(args.sel(), args.max_chars)
        return ToolOutput(
            data={"text": text, "chars": len(text)},
            summary=f"Read {len(text)} chars of window text",
            perception_source="uia",
        )


# ----------------------------------------------------------------------------- click / type / key / focus / scroll
class ClickInput(ElementArgs):
    vision_ref: str | None = Field(
        default=None,
        description="FALLBACK: element ref from vision_analyze (e.g. 'vis_ab12:e2'); only if UI Automation cannot find the control",
    )

    @model_validator(mode="after")
    def _one(self) -> ClickInput:
        if not (self.ref or self.query or self.automation_id or self.vision_ref):
            raise ValueError("provide 'ref', 'query', 'automation_id' or 'vision_ref'")
        return self


class DesktopClick(Tool[ClickInput]):
    name = "desktop_click"
    description = "Click a control via UI Automation (Invoke/Toggle/Select patterns, then element-relative mouse). vision_ref uses coordinates from a confident vision result as a last resort."
    capability = Capability.DESKTOP
    Input = ClickInput
    timeout_s = 45.0
    retry = _RETRY_STALE

    async def assess(self, args: ClickInput, ctx: ExecutionContext) -> RiskAssessment:  # type: ignore[override]
        await _window_policy(ctx, args.sel(), for_input=True)
        el = _peek(ctx, args.ref)
        label = (
            (el.name if el else None) or args.query or args.automation_id or args.vision_ref or ""
        )
        if args.vision_ref:
            return RiskAssessment(
                risk=RiskLevel.MEDIUM, action=f"Click screen coordinates from vision result {args.vision_ref}", resource=args.vision_ref,
                reasons=["Coordinate clicks rely on a vision model's estimate rather than UI structure"], category="coordinate_click",
            )  # fmt: skip
        if SENSITIVE_ACTION.search(label):
            return RiskAssessment(
                risk=RiskLevel.HIGH, action=f'Click "{label}" in {args.window_title or "the active window"}', resource=args.window_title or "active window",
                reasons=["This control may submit, install, delete, send or authorise something"], category="sensitive_ui_action",
            )  # fmt: skip
        return RiskAssessment(
            risk=RiskLevel.LOW,
            action=f'Click "{label}"',
            resource=args.window_title or "active window",
            category="desktop_interact",
        )

    async def run(self, args: ClickInput, ctx: ExecutionContext) -> ToolOutput:
        svc = _svc(ctx)
        if args.vision_ref and not (args.ref or args.query or args.automation_id):
            vision = ctx.services.vision
            if vision is None:
                raise AgentError("Vision is not enabled", code=ErrorCode.NOT_AVAILABLE)
            x, y, meta, vel = vision.resolve_point(
                ctx.task_id, args.vision_ref
            )  # raises LOW_CONFIDENCE
            sel = WindowSel(handle=meta.window_handle) if meta.window_handle else args.sel()
            await svc.click_point(x, y, sel=sel)
            return ToolOutput(
                data={"method": "coordinates", "point": [x, y], "label": vel.label}, summary=f'Clicked "{vel.label}" at ({x},{y}) from vision result (confidence {vel.confidence:.2f})',
                perception_source="vision",
            )  # fmt: skip
        el = await _element(ctx, args)
        outcome = await svc.click(el)
        return ToolOutput(
            data={"method": outcome.method, "element": el.brief()},
            summary=f"Clicked {el.brief()} via {outcome.method}",
            perception_source="uia",
            artifacts={"element": el.name},
        )


class TypeInput(ElementArgs):
    text: str = Field(max_length=5000)
    clear: bool = True

    @model_validator(mode="after")
    def _one(self) -> TypeInput:
        if not (self.ref or self.query or self.automation_id):
            raise ValueError("provide 'ref', 'query' or 'automation_id' of the text control")
        return self


class DesktopType(Tool[TypeInput]):
    name = "desktop_type"
    description = "Type text into a specific text control (set via UI Automation value pattern; keyboard only if needed). Terminals are blocked."
    capability = Capability.DESKTOP
    Input = TypeInput
    risk = RiskLevel.MEDIUM
    timeout_s = 45.0
    retry = _RETRY_STALE

    async def assess(self, args: TypeInput, ctx: ExecutionContext) -> RiskAssessment:  # type: ignore[override]
        await _window_policy(ctx, args.sel(), for_input=True)
        el = _peek(ctx, args.ref)
        if el is not None and el.sensitive:
            return RiskAssessment(
                risk=RiskLevel.HIGH, action=f'Type a value into the sensitive field "{el.name}"', resource=args.window_title or "active window",
                reasons=["The control looks like a password/secret field; the typed value is masked in all logs"],
                category="credential_entry", scoped_grant_allowed=False, mask_args=["text"],
            )  # fmt: skip
        label = (el.name if el else None) or args.query or args.automation_id or args.ref or ""
        return RiskAssessment(
            risk=RiskLevel.MEDIUM, action=f'Type {len(args.text)} characters into "{label}"', resource=args.window_title or "active window",
            reasons=["Changes the content of an application"], category="desktop_input",
        )  # fmt: skip

    async def run(self, args: TypeInput, ctx: ExecutionContext) -> ToolOutput:
        svc = _svc(ctx)
        el = await _element(ctx, args)
        if el.role not in {"textbox", "document", "combobox"}:
            raise AgentError(
                f"{el.brief()} is not a text control; refusing to type into it",
                code=ErrorCode.INVALID_INPUT,
            )
        method = await svc.type_text(el, args.text, clear=args.clear)
        shown = "***" if el.sensitive else (args.text[:50] + ("…" if len(args.text) > 50 else ""))
        return ToolOutput(
            data={"method": method, "element": el.brief()},
            summary=f'Typed "{shown}" into {el.brief()} ({method})',
            perception_source="uia",
        )


class KeyInput(WinArgs):
    keys: str = Field(
        description="One combination, e.g. 'Ctrl+S', 'Ctrl+Shift+P', 'Enter', 'F5'. The Windows key is not allowed."
    )
    focus_first: bool = Field(
        default=False, description="Bring the selected window to the foreground first"
    )


class DesktopKey(Tool[KeyInput]):
    name = "desktop_key"
    description = "Send ONE validated key combination to the focused window (e.g. Ctrl+S). Terminals and the Windows key are blocked."
    capability = Capability.DESKTOP
    Input = KeyInput
    risk = RiskLevel.MEDIUM
    timeout_s = 30.0

    async def assess(self, args: KeyInput, ctx: ExecutionContext) -> RiskAssessment:  # type: ignore[override]
        parsed = keymod.parse_combo(args.keys)  # raises PolicyViolation
        if args.window_title or args.window_process or args.window_handle:
            await _window_policy(ctx, args.sel(), for_input=True)
        svc = ctx.services.desktop
        if svc and svc.available() and not args.focus_first:
            active = await svc.active()
            if active and (reason := svc.blocked_reason(active, for_input=True)):
                raise PolicyViolation(reason)
        risky = keymod.is_risky(parsed)
        return RiskAssessment(
            risk=RiskLevel.HIGH if risky else RiskLevel.MEDIUM, action=f"Press {args.keys}", resource=args.window_title or "focused window",
            reasons=["This shortcut can close windows or discard data"] if risky else ["Keystrokes act on whatever window has focus"],
            category="desktop_key_risky" if risky else "desktop_input",
        )  # fmt: skip

    async def run(self, args: KeyInput, ctx: ExecutionContext) -> ToolOutput:
        svc = _svc(ctx)
        parsed = keymod.parse_combo(args.keys)
        if args.focus_first:
            await svc.focus(args.sel())
        active = await svc.send_keys(parsed)
        return ToolOutput(
            data={"keys": args.keys, "window": active.title if active else None},
            summary=f"Pressed {args.keys}",
            perception_source="uia",
        )


class FocusInput(WinArgs):
    pass


class DesktopFocus(Tool[FocusInput]):
    name = "desktop_focus"
    description = "Bring a window to the foreground (restores it if minimized)."
    capability = Capability.DESKTOP
    Input = FocusInput
    timeout_s = 20.0

    async def assess(self, args: FocusInput, ctx: ExecutionContext) -> RiskAssessment:  # type: ignore[override]
        await _window_policy(ctx, args.sel(), for_input=False)
        return RiskAssessment(
            risk=RiskLevel.LOW,
            action=f"Focus window {args.window_title or args.window_process or args.window_handle}",
            resource=args.window_title or "window",
            category="desktop_interact",
        )

    async def run(self, args: FocusInput, ctx: ExecutionContext) -> ToolOutput:
        win = await _svc(ctx).focus(args.sel())
        return ToolOutput(
            data={"window": win.title, "process": win.process},
            summary=f"Focused '{win.title[:60]}'",
            perception_source="uia",
        )


class ScrollInput(ElementArgs):
    direction: str = Field(default="down", pattern="^(up|down|left|right)$")
    amount: int = Field(default=3, ge=1, le=20)


class DesktopScroll(Tool[ScrollInput]):
    name = "desktop_scroll"
    description = (
        "Scroll a control or window (UI Automation scroll pattern, mouse wheel as fallback)."
    )
    capability = Capability.DESKTOP
    Input = ScrollInput
    timeout_s = 20.0

    async def assess(self, args: ScrollInput, ctx: ExecutionContext) -> RiskAssessment:  # type: ignore[override]
        await _window_policy(ctx, args.sel(), for_input=True)
        return RiskAssessment(
            risk=RiskLevel.LOW,
            action=f"Scroll {args.direction} x{args.amount}",
            resource=args.window_title or "active window",
            category="desktop_interact",
        )

    async def run(self, args: ScrollInput, ctx: ExecutionContext) -> ToolOutput:
        svc = _svc(ctx)
        el = await _element(ctx, args) if (args.ref or args.query or args.automation_id) else None
        method = await svc.scroll(el, args.sel(), args.direction, args.amount)
        return ToolOutput(
            data={"method": method},
            summary=f"Scrolled {args.direction} ({method})",
            perception_source="uia",
        )


class LaunchInput(BaseModel):
    app: str = Field(
        description="Allowlisted application name, e.g. 'vscode', 'notepad', 'calculator', 'explorer'"
    )
    args: list[str] = Field(
        default_factory=list,
        description="Arguments; file/folder paths must be inside allowed folders",
    )


class DesktopLaunch(Tool[LaunchInput]):
    name = "desktop_launch"
    description = (
        "Launch an allowlisted application (optionally opening a folder/file inside allowed roots)."
    )
    capability = Capability.DESKTOP
    Input = LaunchInput
    risk = RiskLevel.MEDIUM
    timeout_s = 30.0

    def assess(self, args: LaunchInput, ctx: ExecutionContext) -> RiskAssessment:
        cfg = ctx.settings.desktop
        if args.app.lower() not in cfg.apps:
            raise PolicyViolation(
                f"'{args.app}' is not launchable. Allowed: {', '.join(sorted(cfg.apps))}"
            )
        for a in args.args:
            if a.startswith("-"):
                if a.startswith(
                    (
                        "--install-extension",
                        "--user-data-dir",
                        "--extensions-dir",
                        "--remote",
                        "--tunnel",
                    )
                ):
                    raise PolicyViolation(f"Launch option {a.split('=')[0]} is not allowed")
                continue
            ctx.paths.resolve(a, cwd=ctx.cwd)
        return RiskAssessment(
            risk=RiskLevel.MEDIUM, action=f"Launch {args.app} {' '.join(args.args)}".strip(), resource=args.app, category="open_application",
            reasons=["Starts a program on your computer"],
        )  # fmt: skip

    async def run(self, args: LaunchInput, ctx: ExecutionContext) -> ToolOutput:
        svc = _svc(ctx)
        resolved = [
            str(ctx.paths.resolve(a, cwd=ctx.cwd).path) if not a.startswith("-") else a
            for a in args.args
        ]
        info = await svc.launch(args.app, resolved)
        win = info.get("window")
        return ToolOutput(
            data=info, summary=f"Launched {args.app}" + (f"; window '{win['title'][:50]}' appeared" if win else "; no new window detected yet"),
            perception_source="uia", artifacts={"app": args.app, "pid": info["pid"]},
        )  # fmt: skip


DESKTOP_TOOLS: list[type[Tool[Any]]] = [
    DesktopWindows, DesktopActiveWindow, DesktopFindElement, DesktopRead, DesktopClick, DesktopType,
    DesktopKey, DesktopFocus, DesktopScroll, DesktopLaunch,
]  # fmt: skip
