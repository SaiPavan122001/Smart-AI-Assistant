"""Real Windows UI Automation. Read-only tests always run on Windows; the Notepad test is opt-in
(``SAA_UIA_INTERACTIVE=1``) because it opens, drives and closes a visible window."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest
from app.models.tool import ToolCall
from app.tools.desktop.service import DesktopService
from app.tools.desktop.uia import UIABackend

from tests.helpers import Env, make_env

pytestmark = [
    pytest.mark.windows,
    pytest.mark.skipif(os.name != "nt", reason="Windows UI Automation"),
]


@pytest.fixture
def wenv(tmp_path: Path) -> Env:
    env = make_env(tmp_path, include_desktop=True)
    svc = DesktopService(UIABackend(), env.settings.desktop)
    if not svc.available():
        pytest.skip("pywinauto unavailable")
    env.services.desktop = svc
    return env


async def run(env: Env, tool: str, **args: Any):  # type: ignore[no-untyped-def]
    return await env.router.execute(ToolCall(tool=tool, args=args, rationale="test"), env.ctx())


async def test_enumerates_real_windows_read_only(wenv: Env) -> None:
    r = await run(wenv, "desktop_windows")
    assert r.ok and r.data["windows"], "a Windows desktop always has at least the shell window"
    assert all({"handle", "title", "process"} <= set(w) for w in r.data["windows"])
    active = await run(wenv, "desktop_active_window")
    assert active.ok


async def test_reads_real_ui_tree_structurally(wenv: Env) -> None:
    wins = (await run(wenv, "desktop_windows")).data["windows"]
    shell = next((w for w in wins if w["process"].lower() == "explorer.exe"), wins[0])
    tree = await run(
        wenv, "desktop_read", mode="tree", window_handle=shell["handle"], max_elements=40
    )
    assert tree.ok and tree.data["tree"], "UIA returns structured controls, no screenshot involved"
    assert tree.perception_source == "uia" and tree.data["quality"] in {"good", "poor", "empty"}


async def test_protected_real_windows_are_not_listed(wenv: Env) -> None:
    wins = (await run(wenv, "desktop_windows")).data["windows"]
    titles = " ".join(w["title"].lower() for w in wins)
    assert "windows security" not in titles and "keepass" not in titles


@pytest.mark.skipif(
    os.environ.get("SAA_UIA_INTERACTIVE") != "1", reason="opens a visible Notepad window"
)
async def test_drives_real_notepad_via_uia(wenv: Env) -> None:
    proc = subprocess.Popen(["notepad.exe"])  # noqa: S603, S607
    try:
        for _ in range(40):
            wins = (await run(wenv, "desktop_windows")).data["windows"]
            if any(w["process"].lower() == "notepad.exe" for w in wins):
                break
            time.sleep(0.25)
        else:
            pytest.skip("Notepad window did not appear")
        found = await run(
            wenv,
            "desktop_find_element",
            window_process="notepad.exe",
            query="Text editor",
            role="document",
        )
        if not found.ok:
            found = await run(
                wenv,
                "desktop_find_element",
                window_process="notepad.exe",
                role="textbox",
                query="editor",
            )
        assert found.ok, found.error
        ref = found.data["matches"][0]["ref"]
        typed = await run(
            wenv, "desktop_type", window_process="notepad.exe", ref=ref, text="hello from the agent"
        )
        assert typed.ok, typed.error
        # verification contracts poll (wait_s): UIA state can lag the keyboard path by a moment
        verified = await run(
            wenv,
            "verify_desktop_state",
            window_process="notepad.exe",
            element_query="Text editor",
            element_role="document",
            expect_value_contains="hello from the agent",
        )
        deadline = time.time() + 5
        while not verified.data.get("passed") and time.time() < deadline:
            time.sleep(0.3)
            verified = await run(
                wenv,
                "verify_desktop_state",
                window_process="notepad.exe",
                element_query="Text editor",
                element_role="document",
                expect_value_contains="hello from the agent",
            )
        assert verified.data["passed"], verified.data
    finally:
        subprocess.run(["taskkill", "/IM", "notepad.exe", "/F"], capture_output=True, check=False)  # noqa: S603, S607
        proc.kill()
