"""One isolated browser context per task, with cached structured page state."""

from __future__ import annotations

import contextlib
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urljoin, urlsplit

from playwright.async_api import BrowserContext, Download, Locator, Page, Route
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeout
from pydantic import BaseModel, Field

from app.config.settings import BrowserSettings
from app.observability.logging import get_logger
from app.security.paths import PathPolicy
from app.security.urls import check_url
from app.tools.browser.elements import (
    EXTRACT_JS,
    ElementInfo,
    is_ambiguous,
    score_elements,
)
from app.utils.errors import AgentError, ErrorCode, UrlViolation

log = get_logger("browser")

_SAFE_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


class BrowserState(BaseModel):
    """Cached, compact description of the browser. This (not the page) goes to the LLM."""

    current_url: str = ""
    page_title: str = ""
    active_tab: str | None = None
    tab_count: int = 0
    known_elements: dict[str, ElementInfo] = Field(default_factory=dict)
    last_action: str = ""
    last_observation: str = ""
    navigation_state: str = "idle"  # idle | loading | loaded | failed
    generation: int = 0  # bumped on every navigation / refresh; stale refs detected against it
    elements_generation: int = -1


class BrowserSession:
    def __init__(
        self,
        task_id: str,
        context: BrowserContext,
        settings: BrowserSettings,
        path_policy: PathPolicy,
    ) -> None:
        self.task_id = task_id
        self._ctx = context
        self._settings = settings
        self._paths = path_policy
        self._pages: dict[str, Page] = {}
        self._tab_seq = 0
        self._ref_seq = 0
        self.active: str | None = None
        self.state = BrowserState()
        self._downloads: list[dict[str, Any]] = []
        context.set_default_timeout(settings.default_timeout_ms)
        context.on("page", self._on_new_page)

    # ------------------------------------------------------------------ tabs
    def _register(self, page: Page) -> str:
        for tid, p in self._pages.items():
            if p is page:
                return tid
        self._tab_seq += 1
        tid = f"t{self._tab_seq}"
        self._pages[tid] = page
        page.on("close", lambda _p=page, _t=tid: self._on_close(_t))
        page.on("framenavigated", lambda frame, _p=page: self._on_nav(_p, frame))
        return tid

    def _on_new_page(self, page: Page) -> None:
        tid = self._register(page)
        if len(self._pages) > self._settings.max_tabs:
            log.warning("tab limit exceeded; closing newest tab", extra={"tab": tid})
            self._pages.pop(tid, None)
            self._spawn(page.close())
            return
        self.active = tid  # popups (target=_blank) become the active tab
        self.state.generation += 1

    def _on_close(self, tid: str) -> None:
        self._pages.pop(tid, None)
        if self.active == tid:
            self.active = next(iter(self._pages), None)
        self.state.generation += 1

    def _on_nav(self, page: Page, frame: Any) -> None:
        if frame == page.main_frame:
            self.state.generation += 1

    @staticmethod
    def _spawn(coro: Any) -> None:
        import asyncio

        task = asyncio.ensure_future(coro)
        task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)

    @property
    def page(self) -> Page:
        if self.active is None or self.active not in self._pages:
            raise AgentError(
                "No open browser tab. Call browser_open first.", code=ErrorCode.NOT_FOUND
            )
        return self._pages[self.active]

    @property
    def has_page(self) -> bool:
        return self.active is not None and self.active in self._pages

    async def open_tab(self, url: str | None = None) -> str:
        if len(self._pages) >= self._settings.max_tabs:
            raise AgentError(
                f"Tab limit reached ({self._settings.max_tabs}). Close a tab first.",
                code=ErrorCode.LIMIT_EXCEEDED,
            )
        page = await self._ctx.new_page()
        tid = self._register(page)
        self.active = tid
        if url:
            await self.navigate(url)
        else:
            await self.refresh_state()
        return tid

    async def switch_tab(self, tab_id: str) -> None:
        if tab_id not in self._pages:
            raise AgentError(f"Unknown tab '{tab_id}'", code=ErrorCode.NOT_FOUND)
        self.active = tab_id
        await self._pages[tab_id].bring_to_front()
        self.state.generation += 1
        await self.refresh_state()

    async def close_tab(self, tab_id: str | None = None) -> None:
        tid = tab_id or self.active
        if tid is None or tid not in self._pages:
            raise AgentError("No such tab", code=ErrorCode.NOT_FOUND)
        await self._pages[tid].close()
        await self.refresh_state()

    async def tabs(self) -> list[dict[str, Any]]:
        out = []
        for tid, p in self._pages.items():
            title = ""
            with contextlib.suppress(PlaywrightError):
                title = await p.title()
            out.append({"id": tid, "url": p.url, "title": title, "active": tid == self.active})
        return out

    # ------------------------------------------------------------------ navigation
    async def navigate(self, url: str) -> dict[str, Any]:
        safe = check_url(
            url, allow_private_network=self._settings.allow_private_network, resolve_dns=True,
            allowed_schemes=("http", "https"),
        )  # fmt: skip
        page = self.page
        self.state.navigation_state = "loading"
        try:
            resp = await page.goto(safe, wait_until="domcontentloaded")
        except PlaywrightTimeout as exc:
            self.state.navigation_state = "failed"
            raise AgentError(
                f"Navigation to {safe} timed out", code=ErrorCode.NAVIGATION_FAILED, retryable=True
            ) from exc
        except PlaywrightError as exc:
            self.state.navigation_state = "failed"
            msg = str(exc).splitlines()[0][:200]
            if "Download is starting" in msg:
                raise AgentError(
                    f"{safe} is a file download, not a page. Use browser_download with this URL.",
                    code=ErrorCode.NAVIGATION_FAILED,
                ) from exc
            raise AgentError(
                f"Navigation failed: {msg}",
                code=ErrorCode.NAVIGATION_FAILED,
                retryable="net::" in msg,
            ) from exc
        await self._settle()
        self.state.navigation_state = "loaded"
        self.state.last_action = f"navigate {safe}"
        status = resp.status if resp else None
        await self.refresh_state()
        return {"status": status, **self.compact_state()}

    async def history(self, direction: int) -> dict[str, Any]:
        page = self.page
        try:
            await (page.go_back() if direction < 0 else page.go_forward())
        except PlaywrightError as exc:
            raise AgentError(
                f"History navigation failed: {str(exc).splitlines()[0][:150]}",
                code=ErrorCode.NAVIGATION_FAILED,
            ) from exc
        await self._settle()
        self.state.last_action = "back" if direction < 0 else "forward"
        await self.refresh_state()
        return self.compact_state()

    async def _settle(self) -> None:
        page = self.page
        with contextlib.suppress(PlaywrightTimeout, PlaywrightError):
            await page.wait_for_load_state("load", timeout=4000)
        with contextlib.suppress(PlaywrightTimeout, PlaywrightError):
            await page.wait_for_load_state("networkidle", timeout=1200)

    # ------------------------------------------------------------------ state
    async def refresh_state(self) -> BrowserState:
        s = self.state
        s.tab_count = len(self._pages)
        s.active_tab = self.active
        if self.has_page:
            p = self.page
            s.current_url = p.url
            with contextlib.suppress(PlaywrightError):
                s.page_title = await p.title()
        else:
            s.current_url, s.page_title = "", ""
        return s

    def compact_state(self) -> dict[str, Any]:
        s = self.state
        return {
            "url": s.current_url, "title": s.page_title, "tab": s.active_tab, "tabs": s.tab_count,
            "navigation": s.navigation_state,
        }  # fmt: skip

    def peek_element(self, ref: str) -> ElementInfo | None:
        return self.state.known_elements.get(ref)

    # ------------------------------------------------------------------ elements
    async def refresh_elements(self) -> list[ElementInfo]:
        page = self.page
        raw = await page.evaluate(EXTRACT_JS, self._ref_seq + 1)
        self._ref_seq += len(raw)
        elements = [
            ElementInfo(**{k: v for k, v in r.items() if k in ElementInfo.model_fields})
            for r in raw
        ]
        s = self.state
        s.known_elements = {e.ref: e for e in elements}
        s.elements_generation = s.generation
        return elements

    async def _elements(self, *, force: bool = False) -> list[ElementInfo]:
        s = self.state
        if force or s.elements_generation != s.generation or not s.known_elements:
            return await self.refresh_elements()
        return list(s.known_elements.values())

    async def find(self, query: str, role: str | None = None, limit: int = 5) -> dict[str, Any]:
        elements = await self._elements()
        ranked = score_elements(elements, query, role)
        if not ranked:  # one forced refresh: dynamic pages may have changed without navigation
            elements = await self._elements(force=True)
            ranked = score_elements(elements, query, role)
        top = ranked[:limit]
        return {
            "query": query,
            "matches": [
                {
                    "ref": e.ref,
                    "role": e.role,
                    "name": e.name or e.placeholder or "",
                    "score": e.score,
                    "disabled": e.disabled,
                    "href": e.href,
                }
                for e in top
            ],  # fmt: skip
            "ambiguous": is_ambiguous(ranked),
            "total_interactive": len(elements),
        }

    async def resolve_target(
        self, *, ref: str | None, query: str | None, role: str | None
    ) -> tuple[Locator, ElementInfo | None]:
        """Turn a ref or a natural-language query into exactly one live locator."""
        page = self.page
        if ref:
            known = self.state.known_elements.get(ref)
            loc = page.locator(f'[data-saa-ref="{ref}"]')
            try:
                count = await loc.count()
            except PlaywrightError:
                count = 0
            if known is None or count != 1:
                # stale: refresh once and try to re-find the same element by identity
                old = known
                elements = await self.refresh_elements()
                if old is not None:
                    twin = _best_twin(old, elements)
                    if twin is not None:
                        return page.locator(f'[data-saa-ref="{twin.ref}"]'), twin
                raise AgentError(
                    f"Element reference '{ref}' is stale (the page changed). State was refreshed; "
                    "call browser_find again to get current refs.",
                    code=ErrorCode.STALE_REFERENCE, retryable=True,
                )  # fmt: skip
            return loc, known
        if not query:
            raise AgentError("Provide 'ref' or 'query'", code=ErrorCode.INVALID_INPUT)
        found = await self.find(query, role, limit=4)
        matches = found["matches"]
        if not matches:
            raise AgentError(
                f"No element matches '{query}'" + (f" (role {role})" if role else ""),
                code=ErrorCode.ELEMENT_NOT_FOUND, retryable=True,
                details={"hint": "Use browser_read(mode='elements') to list what is on the page"},
            )  # fmt: skip
        if found["ambiguous"]:
            raise AgentError(
                f"'{query}' matches several elements equally well; pick one by ref.",
                code=ErrorCode.AMBIGUOUS_TARGET, details={"candidates": matches},
            )  # fmt: skip
        el = self.state.known_elements[matches[0]["ref"]]
        return page.locator(f'[data-saa-ref="{el.ref}"]'), el

    # ------------------------------------------------------------------ actions
    async def click(self, loc: Locator, el: ElementInfo | None) -> dict[str, Any]:
        before = self.page.url
        try:
            await loc.scroll_into_view_if_needed(timeout=5000)
            await loc.click(timeout=self._settings.default_timeout_ms)
        except PlaywrightTimeout as exc:
            raise AgentError(
                "Click timed out (element not clickable, covered, or disabled)",
                code=ErrorCode.ELEMENT_NOT_FOUND,
                retryable=True,
            ) from exc
        except PlaywrightError as exc:
            raise AgentError(
                f"Click failed: {str(exc).splitlines()[0][:160]}",
                code=ErrorCode.ELEMENT_NOT_FOUND,
                retryable=True,
            ) from exc
        await self._settle()
        self.state.generation += 1
        self.state.last_action = f"click {el.brief() if el else ''}".strip()
        await self.refresh_state()
        return {
            "navigated": self.page.url != before if self.has_page else True,
            **self.compact_state(),
        }

    async def type_text(
        self, loc: Locator, text: str, *, clear: bool, submit: bool
    ) -> dict[str, Any]:
        try:
            if clear:
                await loc.fill(text, timeout=self._settings.default_timeout_ms)
            else:
                await loc.focus()
                await self.page.keyboard.type(text, delay=5)
            if submit:
                await loc.press("Enter")
                await self._settle()
        except PlaywrightError as exc:
            raise AgentError(
                f"Typing failed: {str(exc).splitlines()[0][:160]}",
                code=ErrorCode.ELEMENT_NOT_FOUND,
                retryable=True,
            ) from exc
        self.state.generation += 1 if submit else 0
        self.state.last_action = "type" + (" + submit" if submit else "")
        await self.refresh_state()
        return self.compact_state()

    async def select_option(
        self, loc: Locator, *, value: str | None, label: str | None
    ) -> dict[str, Any]:
        try:
            if label is not None:
                await loc.select_option(label=label)
            elif value is not None:
                await loc.select_option(value=value)
            else:
                raise AgentError("Provide 'value' or 'label'", code=ErrorCode.INVALID_INPUT)
        except PlaywrightError as exc:
            raise AgentError(
                f"Select failed: {str(exc).splitlines()[0][:160]}",
                code=ErrorCode.ELEMENT_NOT_FOUND,
                retryable=True,
            ) from exc
        self.state.last_action = "select"
        return self.compact_state()

    async def upload(self, loc: Locator, file_path: Path) -> None:
        await loc.set_input_files(str(file_path))

    # ------------------------------------------------------------------ reading
    async def read(self, *, mode: str, selector: str | None, max_chars: int) -> dict[str, Any]:
        page = self.page
        data: dict[str, Any] = {**self.compact_state(), "mode": mode}
        if mode == "text":
            target = page.locator(selector).first if selector else page.locator("body")
            try:
                text = await target.inner_text(timeout=5000)
            except PlaywrightError as exc:
                raise AgentError(
                    f"Could not read text: {str(exc).splitlines()[0][:120]}",
                    code=ErrorCode.ELEMENT_NOT_FOUND,
                ) from exc
            text = re.sub(r"\n{3,}", "\n\n", text).strip()
            data["text"] = text[:max_chars]
            data["truncated"] = len(text) > max_chars
            data["total_chars"] = len(text)
        elif mode == "aria":
            target = page.locator(selector).first if selector else page.locator("body")
            try:
                snap = await target.aria_snapshot(timeout=5000)
            except PlaywrightError as exc:
                raise AgentError(
                    f"Accessibility snapshot failed: {str(exc).splitlines()[0][:120]}",
                    code=ErrorCode.NOT_AVAILABLE,
                ) from exc
            data["aria"] = snap[:max_chars]
            data["truncated"] = len(snap) > max_chars
        elif mode in {"elements", "links", "forms"}:
            elements = await self._elements(force=True)
            if mode == "links":
                elements = [e for e in elements if e.role == "link"]
            elif mode == "forms":
                elements = [
                    e
                    for e in elements
                    if e.role
                    in {
                        "textbox",
                        "combobox",
                        "checkbox",
                        "radio",
                        "button",
                        "slider",
                        "filebutton",
                    }
                ]
            data["elements"] = [e.brief() for e in elements[:120]]
            data["total"] = len(elements)
        else:
            raise AgentError(f"Unknown read mode '{mode}'", code=ErrorCode.INVALID_INPUT)
        self.state.last_observation = f"read {mode}"
        return data

    async def screenshot(self, *, full_page: bool = False) -> bytes:
        return await self.page.screenshot(full_page=full_page, type="png")

    # ------------------------------------------------------------------ downloads
    def _dest_dir(self, dest_dir: str | None) -> Path:
        raw = dest_dir or "@downloads"
        rp = self._paths.resolve(raw, write=True)
        rp.path.mkdir(parents=True, exist_ok=True)
        return rp.path

    def _unique_path(self, directory: Path, filename: str) -> Path:
        name = _SAFE_NAME.sub("_", os.path.basename(filename)).strip(". ") or "download"
        target = self._paths.resolve(
            str(directory / name), write=True
        ).path  # re-validates extension etc.
        stem, suffix = target.stem, target.suffix
        i = 1
        while target.exists():
            target = directory / f"{stem} ({i}){suffix}"
            i += 1
        return target

    async def download_url(
        self, url: str, *, dest_dir: str | None, filename: str | None
    ) -> dict[str, Any]:
        directory = self._dest_dir(dest_dir)
        cur = check_url(
            url, allow_private_network=self._settings.allow_private_network, resolve_dns=True
        )
        resp = None
        for _ in range(6):  # manual redirects so every hop is policy-checked
            resp = await self._ctx.request.get(cur, max_redirects=0, timeout=60000)
            if resp.status in (301, 302, 303, 307, 308) and resp.headers.get("location"):
                cur = check_url(
                    urljoin(cur, resp.headers["location"]),
                    allow_private_network=self._settings.allow_private_network, resolve_dns=True,
                )  # fmt: skip
                continue
            break
        assert resp is not None
        if resp.status >= 400:
            raise AgentError(
                f"Download failed: HTTP {resp.status}",
                code=ErrorCode.NAVIGATION_FAILED,
                retryable=resp.status >= 500,
            )
        clen = resp.headers.get("content-length")
        if clen and clen.isdigit() and int(clen) > self._settings.max_download_bytes:
            raise AgentError(
                "File exceeds the maximum download size", code=ErrorCode.LIMIT_EXCEEDED
            )
        body = await resp.body()
        if len(body) > self._settings.max_download_bytes:
            raise AgentError(
                "File exceeds the maximum download size", code=ErrorCode.LIMIT_EXCEEDED
            )
        name = filename or _filename_from_response(cur, resp.headers.get("content-disposition"))
        target = self._unique_path(directory, name)
        target.write_bytes(body)
        info = {"path": str(target), "size": len(body), "filename": target.name, "url": cur,
                "content_type": resp.headers.get("content-type", "")}  # fmt: skip
        self._downloads.append(info)
        self.state.last_action = f"download {target.name}"
        return info

    async def download_click(
        self, loc: Locator, *, dest_dir: str | None, filename: str | None
    ) -> dict[str, Any]:
        directory = self._dest_dir(dest_dir)
        try:
            async with self.page.expect_download(timeout=30000) as dl_info:
                await loc.click(timeout=self._settings.default_timeout_ms)
            dl: Download = await dl_info.value
        except PlaywrightTimeout as exc:
            raise AgentError(
                "Clicking did not start a download",
                code=ErrorCode.ELEMENT_NOT_FOUND,
                retryable=True,
            ) from exc
        target = self._unique_path(directory, filename or dl.suggested_filename)
        await dl.save_as(str(target))
        size = target.stat().st_size
        if size > self._settings.max_download_bytes:
            target.unlink(missing_ok=True)
            raise AgentError(
                "File exceeds the maximum download size; it was removed",
                code=ErrorCode.LIMIT_EXCEEDED,
            )
        info = {"path": str(target), "size": size, "filename": target.name, "url": dl.url}
        self._downloads.append(info)
        self.state.last_action = f"download {target.name}"
        return info

    @property
    def downloads(self) -> list[dict[str, Any]]:
        return list(self._downloads)

    # ------------------------------------------------------------------ routing / lifecycle
    async def install_guard(self) -> None:
        """Abort any request (incl. sub-resources, redirects) to a blocked destination."""
        allow_private = self._settings.allow_private_network
        blocked = set(self._settings.blocked_schemes)

        async def handler(route: Route) -> None:
            url = route.request.url
            scheme = urlsplit(url).scheme.lower()
            if scheme in ("blob",) or url.startswith("about:blank"):
                await route.continue_()
                return
            if scheme in blocked and scheme != "data":
                await route.abort("blockedbyclient")
                return
            if scheme == "data":
                await route.continue_()
                return
            try:
                check_url(url, allow_private_network=allow_private, resolve_dns=False)
            except UrlViolation:
                await route.abort("blockedbyclient")
                return
            await route.continue_()

        await self._ctx.route("**/*", handler)

    async def close(self) -> None:
        with contextlib.suppress(Exception):
            await self._ctx.close()


def _filename_from_response(url: str, disposition: str | None) -> str:
    if disposition:
        m = re.search(r"filename\*=(?:UTF-8'')?([^;]+)", disposition, re.I) or re.search(
            r'filename="?([^";]+)"?', disposition, re.I
        )
        if m:
            return unquote(m.group(1).strip())
    name = unquote(os.path.basename(urlsplit(url).path))
    return name or "download"


def _best_twin(old: ElementInfo, elements: list[ElementInfo]) -> ElementInfo | None:
    """Find the element in a refreshed list that is the same logical control."""
    candidates = [
        e
        for e in elements
        if e.role == old.role and e.tag == old.tag and (e.name or "") == (old.name or "")
    ]
    if old.href:
        same_href = [e for e in candidates if e.href == old.href]
        candidates = same_href or candidates
    return candidates[0] if len(candidates) == 1 else None
