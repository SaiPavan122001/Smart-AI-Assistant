"""Browser tools (Playwright). Structured DOM/accessibility perception first; no screenshots."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, model_validator

from app.models.enums import Capability, RiskLevel
from app.models.tool import RetryPolicy, RiskAssessment
from app.security.urls import check_url
from app.tools.base import Tool, ToolOutput
from app.tools.browser.elements import SENSITIVE_ACTION
from app.tools.browser.session import BrowserSession
from app.tools.context import ExecutionContext
from app.utils.errors import AgentError, ErrorCode, PolicyViolation

_RETRY_STALE = RetryPolicy(
    max_attempts=2,
    backoff_s=0.3,
    retry_on=[ErrorCode.STALE_REFERENCE.value, ErrorCode.ELEMENT_NOT_FOUND.value],
)


async def _session(ctx: ExecutionContext, *, need_page: bool = True) -> BrowserSession:
    mgr = ctx.services.browser
    if mgr is None:
        raise AgentError(
            "Browser automation is not enabled in this deployment", code=ErrorCode.NOT_AVAILABLE
        )
    session = await mgr.session(ctx.task_id)
    if need_page and not session.has_page:
        raise AgentError("No open browser tab. Call browser_open first.", code=ErrorCode.NOT_FOUND)
    return session


def _peek(ctx: ExecutionContext, ref: str | None) -> Any:
    mgr = ctx.services.browser
    sess = mgr.peek(ctx.task_id) if mgr else None
    return sess.peek_element(ref) if (sess and ref) else None


def _check_url(ctx: ExecutionContext, url: str) -> str:
    return check_url(
        url, allow_private_network=ctx.settings.browser.allow_private_network, resolve_dns=False
    )


class Target(BaseModel):
    ref: str | None = Field(
        default=None, description="Element ref from browser_find/browser_read, e.g. 'e42'"
    )
    query: str | None = Field(
        default=None, description="Natural-language target, e.g. 'Download', 'Search'"
    )
    role: str | None = Field(
        default=None, description="button | link | textbox | checkbox | combobox | radio"
    )

    @model_validator(mode="after")
    def _one(self) -> Target:
        if not self.ref and not self.query:
            raise ValueError("provide 'ref' or 'query'")
        return self


# ----------------------------------------------------------------------------- open / navigate
class OpenInput(BaseModel):
    url: str | None = Field(default=None, description="Optional http(s) URL to open")
    new_tab: bool = False


class BrowserOpen(Tool[OpenInput]):
    name = "browser_open"
    description = "Open the browser (isolated session for this task), optionally at a URL, optionally in a new tab."
    capability = Capability.BROWSER
    Input = OpenInput
    timeout_s = 60.0
    returns_external_content = True
    retry = RetryPolicy(max_attempts=2, backoff_s=1.0, retry_on=[ErrorCode.NAVIGATION_FAILED.value])

    def assess(self, args: OpenInput, ctx: ExecutionContext) -> RiskAssessment:
        if args.url:
            _check_url(ctx, args.url)
        return RiskAssessment(
            risk=RiskLevel.LOW,
            action=f"Open browser{' at ' + args.url if args.url else ''}",
            resource=args.url or "browser",
            category="browser_navigate",
        )

    async def run(self, args: OpenInput, ctx: ExecutionContext) -> ToolOutput:
        session = await _session(ctx, need_page=False)
        if not session.has_page or args.new_tab:
            await session.open_tab(args.url)
        elif args.url:
            await session.navigate(args.url)
        await session.refresh_state()
        st = session.compact_state()
        return ToolOutput(
            data=st,
            summary=f"Browser open: {st['title'] or '(no title)'} — {st['url'] or 'about:blank'}",
            perception_source="dom",
        )


class NavInput(BaseModel):
    url: str


class BrowserNavigate(Tool[NavInput]):
    name = "browser_navigate"
    description = "Navigate the active tab to an http(s) URL."
    capability = Capability.BROWSER
    Input = NavInput
    timeout_s = 60.0
    returns_external_content = True
    retry = RetryPolicy(max_attempts=2, backoff_s=1.0, retry_on=[ErrorCode.NAVIGATION_FAILED.value])

    def assess(self, args: NavInput, ctx: ExecutionContext) -> RiskAssessment:
        _check_url(ctx, args.url)
        return RiskAssessment(
            risk=RiskLevel.LOW,
            action=f"Navigate to {args.url}",
            resource=args.url,
            category="browser_navigate",
        )

    async def run(self, args: NavInput, ctx: ExecutionContext) -> ToolOutput:
        session = await _session(ctx, need_page=False)
        if not session.has_page:
            await session.open_tab(args.url)
            st = session.compact_state()
        else:
            st = await session.navigate(args.url)
        return ToolOutput(
            data=st,
            summary=f"Navigated: {st['title'] or '(no title)'} — {st['url']}",
            perception_source="dom",
            artifacts={"url": st["url"]},
        )


# ----------------------------------------------------------------------------- find / read
class FindInput(BaseModel):
    query: str = Field(description="What to find, e.g. 'Download', 'search box', 'Sign in'")
    role: str | None = None
    limit: int = Field(default=5, ge=1, le=15)


class BrowserFind(Tool[FindInput]):
    name = "browser_find"
    description = (
        "Find interactive elements matching a description using the page's DOM/accessibility structure. "
        'Returns a few candidates with stable refs (e.g. button "Download" ref=e42) instead of the whole page.'
    )
    capability = Capability.BROWSER
    Input = FindInput
    read_only = True
    idempotent = True
    returns_external_content = True
    retry = RetryPolicy(max_attempts=2, backoff_s=0.5, retry_on=[ErrorCode.ELEMENT_NOT_FOUND.value])

    async def run(self, args: FindInput, ctx: ExecutionContext) -> ToolOutput:
        session = await _session(ctx)
        res = await session.find(args.query, args.role, args.limit)
        if not res["matches"]:
            raise AgentError(
                f"No element matches '{args.query}'", code=ErrorCode.ELEMENT_NOT_FOUND, retryable=False,
                details={"hint": "Try browser_read(mode='elements') to list controls, or a different wording."},
            )  # fmt: skip
        lines = [f'{m["role"]} "{m["name"][:70]}" ref={m["ref"]}' for m in res["matches"]]
        return ToolOutput(
            data={**res, "page": session.compact_state()},
            summary="; ".join(lines) + (" (ambiguous: pick by ref)" if res["ambiguous"] else ""),
            perception_source="dom",
        )


class ReadInput(BaseModel):
    mode: str = Field(default="text", pattern="^(text|aria|elements|links|forms)$",
                      description="text: visible page text | aria: accessibility tree | elements/links/forms: interactive controls")  # fmt: skip
    selector: str | None = Field(
        default=None, description="Optional CSS selector to scope text/aria"
    )
    max_chars: int = Field(default=6000, ge=200, le=40000)
    force: bool = Field(
        default=False, description="Return full content even if unchanged since the last read"
    )


class BrowserRead(Tool[ReadInput]):
    name = "browser_read"
    description = "Read the current page in a targeted way (text, accessibility tree, or interactive elements). Prefer browser_find for single targets."
    capability = Capability.BROWSER
    Input = ReadInput
    read_only = True
    cacheable = True
    cache_ttl_s = 2.0
    idempotent = True
    returns_external_content = True

    async def run(self, args: ReadInput, ctx: ExecutionContext) -> ToolOutput:
        session = await _session(ctx)
        data = await session.read(mode=args.mode, selector=args.selector, max_chars=args.max_chars)
        subject = f"browser:{session.state.current_url}:{args.mode}:{args.selector or ''}"
        payload = {
            k: v for k, v in data.items() if k not in ("url", "title", "tab", "tabs", "navigation")
        }
        if not args.force and ctx.cache.unchanged(subject, payload):
            return ToolOutput(
                data={"unchanged": True, **session.compact_state()},
                summary=f"Page content unchanged since the last '{args.mode}' read of {session.state.current_url}",
                perception_source="accessibility" if args.mode == "aria" else "dom",
            )
        size = len(data.get("text") or data.get("aria") or "") or len(data.get("elements") or [])
        return ToolOutput(
            data=data, summary=f"Read {args.mode} of '{session.state.page_title or session.state.current_url}' ({size} {'chars' if 'text' in data or 'aria' in data else 'elements'})",
            perception_source="accessibility" if args.mode == "aria" else "dom",
        )  # fmt: skip


# ----------------------------------------------------------------------------- click / type / select
class ClickInput(Target):
    pass


class BrowserClick(Tool[ClickInput]):
    name = "browser_click"
    description = "Click an element by ref (from browser_find) or by natural-language query. Re-resolves stale refs automatically."
    capability = Capability.BROWSER
    Input = ClickInput
    timeout_s = 45.0
    returns_external_content = True
    retry = _RETRY_STALE

    def assess(self, args: ClickInput, ctx: ExecutionContext) -> RiskAssessment:
        el = _peek(ctx, args.ref)
        label = (el.name if el else None) or args.query or args.ref or ""
        if SENSITIVE_ACTION.search(label):
            return RiskAssessment(
                risk=RiskLevel.HIGH, action=f'Click "{label}" on {_page_url(ctx)}', resource=_page_url(ctx),
                reasons=["This control may submit, purchase, send, delete or authorise something"],
                category="sensitive_ui_action",
            )  # fmt: skip
        return RiskAssessment(
            risk=RiskLevel.LOW,
            action=f'Click "{label}"',
            resource=_page_url(ctx),
            category="browser_interact",
        )

    async def run(self, args: ClickInput, ctx: ExecutionContext) -> ToolOutput:
        session = await _session(ctx)
        loc, el = await session.resolve_target(ref=args.ref, query=args.query, role=args.role)
        result = await session.click(loc, el)
        label = el.name if el else (args.query or args.ref)
        return ToolOutput(
            data=result, summary=f'Clicked {el.role if el else "element"} "{(label or "")[:60]}" → {result["url"]}',
            perception_source="dom", artifacts={"url": result["url"], "title": result["title"]},
        )  # fmt: skip


class TypeInput(Target):
    text: str = Field(max_length=5000)
    clear: bool = Field(
        default=True, description="Replace existing content (fill) instead of appending"
    )
    submit: bool = Field(default=False, description="Press Enter afterwards")


class BrowserType(Tool[TypeInput]):
    name = "browser_type"
    description = "Type text into an input/textarea by ref or query. Set submit=true to press Enter (e.g. to run a search)."
    capability = Capability.BROWSER
    Input = TypeInput
    timeout_s = 45.0
    returns_external_content = True
    retry = _RETRY_STALE

    def assess(self, args: TypeInput, ctx: ExecutionContext) -> RiskAssessment:
        el = _peek(ctx, args.ref)
        if el is not None and el.sensitive:
            return RiskAssessment(
                risk=RiskLevel.HIGH, action=f'Type a value into the sensitive field "{el.name or el.placeholder or el.ref}"',
                resource=_page_url(ctx), reasons=["The field looks like a password/secret/payment field; the typed value is masked in all logs"],
                category="credential_entry", scoped_grant_allowed=False, mask_args=["text"],
            )  # fmt: skip
        label = (el.name if el else None) or args.query or args.ref or ""
        return RiskAssessment(
            risk=RiskLevel.LOW,
            action=f'Type into "{label}"',
            resource=_page_url(ctx),
            category="browser_interact",
        )

    async def run(self, args: TypeInput, ctx: ExecutionContext) -> ToolOutput:
        session = await _session(ctx)
        loc, el = await session.resolve_target(
            ref=args.ref, query=args.query, role=args.role or "textbox"
        )
        result = await session.type_text(loc, args.text, clear=args.clear, submit=args.submit)
        masked = bool(el and el.sensitive)
        shown = "***" if masked else (args.text if len(args.text) <= 60 else args.text[:57] + "...")
        return ToolOutput(
            data=result, summary=f'Typed "{shown}" into {el.role if el else "field"} "{((el.name or el.placeholder) if el else args.query) or ""}"' + (" and pressed Enter" if args.submit else ""),
            perception_source="dom",
        )  # fmt: skip


class SelectInput(Target):
    value: str | None = None
    label: str | None = None


class BrowserSelect(Tool[SelectInput]):
    name = "browser_select"
    description = "Choose an option in a <select>/dropdown by visible label or value."
    capability = Capability.BROWSER
    Input = SelectInput
    timeout_s = 30.0
    returns_external_content = True
    retry = _RETRY_STALE

    async def run(self, args: SelectInput, ctx: ExecutionContext) -> ToolOutput:
        session = await _session(ctx)
        loc, _el = await session.resolve_target(
            ref=args.ref, query=args.query, role=args.role or "combobox"
        )
        result = await session.select_option(loc, value=args.value, label=args.label)
        return ToolOutput(
            data=result, summary=f'Selected "{args.label or args.value}"', perception_source="dom"
        )


# ----------------------------------------------------------------------------- download / upload
class DownloadInput(BaseModel):
    url: str | None = Field(
        default=None, description="Direct file URL (preferred: uses the HTTP API, no UI needed)"
    )
    ref: str | None = Field(
        default=None, description="Element that triggers the download when clicked"
    )
    query: str | None = Field(
        default=None, description="Natural-language description of the download control"
    )
    dest_dir: str | None = Field(
        default=None, description="Folder inside allowed roots; default @Downloads"
    )
    filename: str | None = None

    @model_validator(mode="after")
    def _one(self) -> DownloadInput:
        if not (self.url or self.ref or self.query):
            raise ValueError("provide 'url', 'ref' or 'query'")
        return self


class BrowserDownload(Tool[DownloadInput]):
    name = "browser_download"
    description = "Download a file to an allowed folder, via direct URL (HTTP API) or by clicking a download control. Executable file types are blocked."
    capability = Capability.BROWSER
    Input = DownloadInput
    risk = RiskLevel.MEDIUM
    timeout_s = 120.0
    returns_external_content = True

    def assess(self, args: DownloadInput, ctx: ExecutionContext) -> RiskAssessment:
        if args.url:
            _check_url(ctx, args.url)
        dest = ctx.paths.resolve(args.dest_dir or "@downloads", write=True)
        what = args.url or args.query or args.ref or ""
        return RiskAssessment(
            risk=RiskLevel.MEDIUM, action=f"Download {what} into {dest.path}", resource=str(dest.path),
            reasons=["Saves a file from the internet to your computer"], category="download",
        )  # fmt: skip

    async def run(self, args: DownloadInput, ctx: ExecutionContext) -> ToolOutput:
        session = await _session(ctx, need_page=not args.url)
        if args.url:
            info = await session.download_url(
                args.url, dest_dir=args.dest_dir, filename=args.filename
            )
        else:
            loc, _el = await session.resolve_target(ref=args.ref, query=args.query, role=None)
            info = await session.download_click(loc, dest_dir=args.dest_dir, filename=args.filename)
        return ToolOutput(
            data=info, summary=f"Downloaded {info['filename']} ({info['size']} bytes) to {info['path']}",
            artifacts={"path": info["path"], "size": info["size"], "kind": "file", "url": info.get("url")},
            perception_source="api" if args.url else "dom",
        )  # fmt: skip


class UploadInput(Target):
    path: str


class BrowserUpload(Tool[UploadInput]):
    name = "browser_upload"
    description = "Upload a local file through a file input. Disabled unless explicitly enabled; always requires approval."
    capability = Capability.BROWSER
    Input = UploadInput
    risk = RiskLevel.HIGH
    timeout_s = 60.0

    def assess(self, args: UploadInput, ctx: ExecutionContext) -> RiskAssessment:
        if not ctx.settings.browser.allow_upload:
            raise PolicyViolation(
                "File upload from the browser is disabled (browser.allow_upload=false)"
            )
        rp = ctx.paths.resolve(args.path, must_exist=True, cwd=ctx.cwd)
        return RiskAssessment(
            risk=RiskLevel.HIGH, action=f"Upload {rp.path} to {_page_url(ctx)}", resource=f"{rp.path} → {_page_url(ctx)}",
            reasons=["Sends the contents of a local file to a website"], category="file_upload", scoped_grant_allowed=False,
        )  # fmt: skip

    async def run(self, args: UploadInput, ctx: ExecutionContext) -> ToolOutput:
        if not ctx.settings.browser.allow_upload:
            raise PolicyViolation("File upload from the browser is disabled")
        session = await _session(ctx)
        rp = ctx.paths.resolve(args.path, must_exist=True, cwd=ctx.cwd)
        loc, _el = await session.resolve_target(ref=args.ref, query=args.query, role=None)
        await session.upload(loc, Path(rp.path))
        return ToolOutput(data={"uploaded": rp.path.name}, summary=f"Attached {rp.path.name}")


# ----------------------------------------------------------------------------- tabs / history
class TabsInput(BaseModel):
    action: str = Field(default="list", pattern="^(list|switch|close|new)$")
    tab_id: str | None = None
    url: str | None = None


class BrowserTabs(Tool[TabsInput]):
    name = "browser_tabs"
    description = "List, switch, close or open browser tabs."
    capability = Capability.BROWSER
    Input = TabsInput
    timeout_s = 45.0
    returns_external_content = True

    def assess(self, args: TabsInput, ctx: ExecutionContext) -> RiskAssessment:
        if args.url:
            _check_url(ctx, args.url)
        return RiskAssessment(
            risk=RiskLevel.LOW,
            action=f"Tabs: {args.action}",
            resource=args.tab_id or args.url or "tabs",
            category="browser_navigate",
        )

    async def run(self, args: TabsInput, ctx: ExecutionContext) -> ToolOutput:
        session = await _session(ctx, need_page=args.action != "new")
        if args.action == "switch":
            if not args.tab_id:
                raise AgentError("tab_id required", code=ErrorCode.INVALID_INPUT)
            await session.switch_tab(args.tab_id)
        elif args.action == "close":
            await session.close_tab(args.tab_id)
        elif args.action == "new":
            await session.open_tab(args.url)
        tabs = await session.tabs()
        return ToolOutput(
            data={"tabs": tabs, "active": session.active},
            summary=f"{len(tabs)} tab(s); active {session.active}",
            perception_source="dom",
        )


class NoInput(BaseModel):
    pass


class BrowserBack(Tool[NoInput]):
    name = "browser_back"
    description = "Go back in the active tab's history."
    capability = Capability.BROWSER
    Input = NoInput
    returns_external_content = True

    async def run(self, args: NoInput, ctx: ExecutionContext) -> ToolOutput:
        st = await (await _session(ctx)).history(-1)
        return ToolOutput(data=st, summary=f"Back → {st['url']}", perception_source="dom")


class BrowserForward(Tool[NoInput]):
    name = "browser_forward"
    description = "Go forward in the active tab's history."
    capability = Capability.BROWSER
    Input = NoInput
    returns_external_content = True

    async def run(self, args: NoInput, ctx: ExecutionContext) -> ToolOutput:
        st = await (await _session(ctx)).history(1)
        return ToolOutput(data=st, summary=f"Forward → {st['url']}", perception_source="dom")


def _page_url(ctx: ExecutionContext) -> str:
    mgr = ctx.services.browser
    sess = mgr.peek(ctx.task_id) if mgr else None
    return sess.state.current_url if sess else ""


BROWSER_TOOLS: list[type[Tool[Any]]] = [
    BrowserOpen, BrowserNavigate, BrowserFind, BrowserRead, BrowserClick, BrowserType, BrowserSelect,
    BrowserDownload, BrowserUpload, BrowserTabs, BrowserBack, BrowserForward,
]  # fmt: skip
