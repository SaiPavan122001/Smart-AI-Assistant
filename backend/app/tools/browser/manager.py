"""Browser lifecycle: one Playwright browser process, one isolated context per task."""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from playwright.async_api import Browser, Playwright, async_playwright

from app.config.settings import BrowserSettings
from app.observability.logging import get_logger
from app.security.paths import PathPolicy
from app.tools.browser.session import BrowserSession
from app.utils.errors import AgentError, ErrorCode

log = get_logger("browser")


class BrowserManager:
    def __init__(self, settings: BrowserSettings, path_policy: PathPolicy) -> None:
        self._settings = settings
        self._paths = path_policy
        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        self._sessions: dict[str, BrowserSession] = {}
        self._lock = asyncio.Lock()
        self.channel_used: str | None = None

    async def _ensure_browser(self) -> Browser:
        async with self._lock:
            if self._browser is not None and self._browser.is_connected():
                return self._browser
            if self._pw is None:
                self._pw = await async_playwright().start()
            candidates: list[str | None] = []
            if self._settings.channel:
                candidates.append(self._settings.channel)
            candidates += [
                None,
                "chrome",
                "msedge",
            ]  # bundled Chromium first, then installed browsers
            last: Exception | None = None
            seen: set[str | None] = set()
            for channel in candidates:
                if channel in seen:
                    continue
                seen.add(channel)
                try:
                    kwargs: dict[str, Any] = {
                        "headless": self._settings.headless,
                        "args": [
                            "--disable-extensions",
                            "--no-first-run",
                            "--disable-dev-shm-usage",
                        ],
                    }
                    if channel:
                        kwargs["channel"] = channel
                    self._browser = await self._pw.chromium.launch(**kwargs)
                    self.channel_used = channel or "chromium"
                    log.info("browser launched", extra={"channel": self.channel_used})
                    return self._browser
                except Exception as exc:
                    last = exc
                    log.debug(
                        "browser launch failed", extra={"channel": channel, "error": str(exc)[:200]}
                    )
            raise AgentError(
                "Could not launch a browser. Install one with `playwright install chromium` or set "
                "SAA_BROWSER__CHANNEL=chrome|msedge to use an installed browser. "
                f"Last error: {str(last).splitlines()[0][:160] if last else 'unknown'}",
                code=ErrorCode.NOT_AVAILABLE,
            )

    async def session(self, task_id: str) -> BrowserSession:
        existing = self._sessions.get(task_id)
        if existing is not None:
            return existing
        browser = await self._ensure_browser()
        context = await browser.new_context(  # fresh, isolated profile per task: no shared cookies/storage
            accept_downloads=True,
            service_workers="block",
            viewport={"width": 1280, "height": 900},
            permissions=[],
            java_script_enabled=True,
            ignore_https_errors=False,
        )
        session = BrowserSession(task_id, context, self._settings, self._paths)
        await session.install_guard()
        self._sessions[task_id] = session
        return session

    def peek(self, task_id: str) -> BrowserSession | None:
        return self._sessions.get(task_id)

    async def close_session(self, task_id: str) -> None:
        session = self._sessions.pop(task_id, None)
        if session:
            await session.close()

    async def shutdown(self) -> None:
        for tid in list(self._sessions):
            await self.close_session(tid)
        with contextlib.suppress(Exception):
            if self._browser:
                await self._browser.close()
        with contextlib.suppress(Exception):
            if self._pw:
                await self._pw.stop()
        self._browser = None
        self._pw = None
