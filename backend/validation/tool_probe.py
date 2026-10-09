"""Tool-level probes on the REAL machine, with NO LLM (isolates tool bugs from model behaviour).

python -m validation.tool_probe notepad|calculator|chrome
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path
from typing import Any

from app.config.settings import FilesystemSettings, Settings
from app.container import build_container
from app.models.tool import ToolCall
from app.state.memory import InMemoryRepository
from app.tools.context import ExecutionContext


async def main(which: str) -> None:
    root = Path(tempfile.mkdtemp(prefix="saa-probe-"))
    s = Settings(
        env="test",
        log_json=False,
        log_level="WARNING",
        filesystem=FilesystemSettings(allowed_roots=[root], workspace=root / "w"),
    )
    c = build_container(s, repo=InMemoryRepository())
    from app.agent.state import TaskHandle  # noqa: F401
    from app.observability.usage import UsageMeter
    from app.perception.cache import ObservationCache
    from app.permissions.engine import TaskGrants
    from app.security.injection import InjectionGuard
    from app.utils.control import CancelToken, PauseGate

    async def emit(*a: Any, **k: Any) -> None: ...

    async def st(_s: Any) -> None: ...

    ctx = ExecutionContext(task_id="probe", services=c.services, token=CancelToken(c.kill_switch), pause=PauseGate(), meter=UsageMeter(s.limits),
                           guard=InjectionGuard(), grants=TaskGrants(), cache=ObservationCache(), cwd=root, emit=emit, set_status=st)  # fmt: skip

    async def call(tool: str, **args: Any) -> Any:
        r = await c.router.execute(ToolCall(tool=tool, args=args, rationale="probe"), ctx)
        print(
            f"{tool}({args}) -> ok={r.ok} {(r.summary or (r.error.message if r.error else ''))[:200]}"
        )
        return r

    # approvals auto-approve for probe only (these are my own launches of safe apps)
    async def approver() -> None:
        while True:
            for req in c.approvals.pending():
                from app.models.approval import ApprovalDecision

                await c.approvals.resolve(req.id, ApprovalDecision(approved=True))
            await asyncio.sleep(0.05)

    task = asyncio.create_task(approver())
    try:
        if which == "calculator":
            await call("desktop_launch", app="calculator")
            await asyncio.sleep(1.5)
            r = await call("desktop_read", window_title="Calculator", mode="tree", max_elements=120)
            for line in (r.data.get("tree") or [])[:80]:
                print("  ", line)
        elif which == "notepad":
            await call("desktop_launch", app="notepad")
            await asyncio.sleep(1.5)
            r = await call(
                "desktop_find_element",
                window_process="notepad.exe",
                query="Text editor",
                role="document",
            )
            ref = r.data["matches"][0]["ref"]
            await call(
                "desktop_type",
                window_process="notepad.exe",
                ref=ref,
                text="Hello from my AI assistant.",
            )
            await call(
                "verify_desktop_state",
                window_process="notepad.exe",
                element_query="Text editor",
                element_role="document",
                expect_value_contains="Hello from my AI assistant.",
            )
        elif (
            which == "notepad-find"
        ):  # read-only: how do plausible LLM wordings resolve against real Notepad?
            await call("desktop_launch", app="notepad")
            await asyncio.sleep(2.0)
            for q, role in [
                ("text area", None),
                ("Notepad text area", None),
                ("text editor", None),
                ("editor", None),
                ("text box", "textbox"),
                ("document", None),
                ("Text editor", "document"),
                ("text area", "document"),
            ]:
                await call(
                    "desktop_find_element",
                    window_process="notepad.exe",
                    query=q,
                    **({"role": role} if role else {}),
                )
        elif which == "calc-find":
            await call("desktop_launch", app="calculator")
            await asyncio.sleep(2.0)
            for q in (
                "2",
                "5",
                "multiply",
                "x",
                "×",
                "Multiply by",
                "equals",
                "=",
                "Two",
                "Display",
            ):
                r = await call("desktop_find_element", window_title="Calculator", query=q)
                for m in (r.data.get("matches") or [])[:3]:
                    print("     ", m)
        elif which == "vscode":
            # NEXT-A: what does the REAL VS Code expose through UI Automation? Read-only: an already-open
            # window is inspected as it is (nothing is launched into the user's session); only when none is
            # open, a disposable folder is opened. Only structure is printed (roles, counts, control names).
            wins = await call("desktop_windows")
            code_wins = [
                w
                for w in (wins.data.get("windows") or [])
                if str(w.get("process", "")).lower() == "code.exe"
            ]
            print(f"   VS Code windows already open: {len(code_wins)}")
            if not code_wins:
                proj = root / "w" / "vscode-probe"
                proj.mkdir(parents=True, exist_ok=True)
                (proj / "hello.py").write_text("print('hello from the probe')\n")
                await call("desktop_launch", app="vscode", args=[str(proj)])
                await asyncio.sleep(8.0)  # Electron starts slowly
            r = await call("desktop_read", window_process="Code.exe", mode="tree", max_elements=150)
            tree = r.data.get("tree") or []
            print(
                f"   UIA exposure quality: {r.data.get('quality')}  controls read: {len(tree)}  truncated: {r.data.get('truncated')}"
            )
            roles: dict[str, int] = {}
            for line in tree:
                role = line.strip().split(" ", 1)[0].strip("[]():")
                roles[role] = roles.get(role, 0) + 1
            print("   roles:", dict(sorted(roles.items(), key=lambda kv: -kv[1])[:12]))
            for line in tree[:25]:
                print("   ", line[:70])
            for q in (
                "Explorer",
                "Editor",
                "Terminal",
                "Search",
                "Run and Debug",
                "Source Control",
            ):
                fr = await call("desktop_find_element", window_process="Code.exe", query=q)
                top = (fr.data.get("matches") or [])[:1]
                print(
                    f"   find {q!r}: {len(fr.data.get('matches') or [])} match(es)",
                    [str(m)[:110] for m in top],
                )
        elif which == "chrome":
            await call("browser_open", url="https://www.google.com")
            await call("browser_read", mode="elements")
    finally:
        task.cancel()
        await c.stop()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))
