"""Command line entry point: ``python -m app <command>``.

serve     run the API (and the built UI if present)
migrate   apply database migrations (alembic upgrade head)
doctor    check the environment and explain what is missing
tools     print the tool catalogue (name, capability, risk, approval)
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import os
import platform
import shutil
import sys
from collections.abc import Callable
from typing import Any

from app.config.settings import Settings, get_settings


def _ok(flag: bool) -> str:
    return "OK  " if flag else "FAIL"


async def _db_check(settings: Settings) -> tuple[bool, str]:
    from app.database.repository import SqlRepository
    from app.database.session import create_engine, create_session_factory, is_memory_url

    if is_memory_url(settings.database.url):
        return True, "in-memory (nothing is persisted)"
    engine = create_engine(settings.database)
    repo = SqlRepository(engine, create_session_factory(engine))
    try:
        return await repo.ping(), settings.database.url.split("@")[-1]
    finally:
        await repo.close()


def doctor() -> int:
    settings = get_settings()
    problems = 0
    rows: list[tuple[str, bool, str]] = []

    py_ok = sys.version_info >= (3, 11)
    rows.append(("Python >= 3.11", py_ok, platform.python_version()))
    rows.append(
        (
            "Platform",
            platform.system() == "Windows",
            f"{platform.system()} {platform.release()} (desktop automation needs Windows)",
        )
    )

    from app.container import build_path_policy

    policy = build_path_policy(settings)  # creates the workspace folder if needed
    for root in settings.resolved_allowed_roots():
        rows.append(
            (
                f"Allowed folder exists: {root}",
                root.exists(),
                "" if root.exists() else "create it or change SAA_FILESYSTEM__ALLOWED_ROOTS",
            )
        )
    rows.append(("Workspace folder", policy.workspace.exists(), str(policy.workspace)))

    from app.llm.factory import build_llm_provider

    llm_ok = build_llm_provider(settings) is not None
    rows.append(
        (
            f"LLM provider ({settings.llm.provider}/{settings.llm.model})",
            llm_ok,
            "configured"
            if llm_ok
            else "set SAA_LLM__API_KEY (or ANTHROPIC_API_KEY / OPENAI_API_KEY)",
        )
    )

    for mod, label in (
        ("playwright", "Playwright"),
        ("pywinauto", "pywinauto (Windows UI Automation)"),
        ("pyautogui", "PyAutoGUI (fallback input)"),
        ("send2trash", "send2trash"),
    ):
        rows.append(
            (
                label,
                importlib.util.find_spec(mod) is not None,
                "installed" if importlib.util.find_spec(mod) else f"pip install {mod}",
            )
        )

    browsers = [b for b in ("chrome", "msedge") if _has_browser(b)]
    rows.append(
        (
            "Browser for automation",
            bool(browsers),
            ", ".join(browsers) or "run `playwright install chromium` or install Chrome/Edge",
        )
    )

    try:
        db_ok, detail = asyncio.run(_db_check(settings))
    except Exception as exc:
        db_ok, detail = False, f"{type(exc).__name__}: {str(exc)[:120]}"
    rows.append(("Database", db_ok, detail))

    from app.tools.desktop.uia import UIABackend

    rows.append(
        (
            "Windows UI Automation",
            UIABackend().available(),
            "ready" if UIABackend().available() else "unavailable: desktop tools will be disabled",
        )
    )
    token_set = bool(settings.security.api_token)
    rows.append(
        (
            "API token",
            token_set or settings.env != "prod",
            "set" if token_set else "will be generated at startup (required when SAA_ENV=prod)",
        )
    )

    width = max(len(r[0]) for r in rows)
    for name, ok, detail in rows:
        print(f"[{_ok(ok)}] {name.ljust(width)}  {detail}")
        problems += 0 if ok else 1
    print(f"\n{problems} issue(s) found." if problems else "\nEverything required is available.")
    return 0 if problems == 0 else 1


def _has_browser(channel: str) -> bool:
    paths = {
        "chrome": [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        ],
        "msedge": [
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        ],
    }[channel]
    return any(os.path.exists(p) for p in paths) or shutil.which(channel) is not None


def llm_check(vision: bool) -> int:
    from app.llm.check import run_check
    from app.llm.factory import build_llm_provider

    settings = get_settings()
    provider = build_llm_provider(settings)
    if provider is None:
        print(
            f"error: provider '{settings.llm.provider}' is not configured (missing API key?)",
            file=sys.stderr,
        )
        return 2

    async def go() -> int:
        try:
            rep = await run_check(provider, vision=vision)
        finally:
            await provider.aclose()
        print(f"provider={rep.provider} model={rep.model}")
        print("capabilities: " + ", ".join(f"{k}={v}" for k, v in rep.capabilities.items()))
        for w in rep.warnings:
            print(f"  note: {w}")
        for pr in rep.probes:
            print(
                f"[{'PASS' if pr.ok else 'FAIL'}] {pr.name:42} {pr.latency_ms:7.0f} ms  in={pr.input_tokens} out={pr.output_tokens}  {pr.detail}"
            )
        print(
            "\nRESULT: "
            + (
                "the model can power the agent"
                if rep.ok
                else "the model FAILED conformance; do not use it for real tasks"
            )
        )
        return 0 if rep.ok else 1

    return asyncio.run(go())


def print_providers() -> int:
    from app.llm.registry import get_provider_spec, provider_names

    for n in provider_names():
        sp = get_provider_spec(n)
        print(f"{n:18} keys: {', '.join(sp.key_env) or '(none)':32} {sp.description}")
    return 0


def print_tools() -> int:
    from app.container import build_container

    c = build_container(get_settings())
    print(f"{'tool':26} {'capability':13} {'risk':9} {'approval':9} description")
    for spec in sorted(c.registry.specs(), key=lambda s: (s.capability.value, s.name)):
        print(
            f"{spec.name:26} {spec.capability.value:13} {spec.risk.label:9} {spec.approval.value:9} {spec.description[:70]}"
        )
    return 0


async def _preflight(settings: Settings) -> str | None:
    from app.container import database_problem
    from app.database.repository import SqlRepository
    from app.database.session import create_engine, create_session_factory, is_memory_url

    if is_memory_url(settings.database.url):
        return None
    engine = create_engine(settings.database)
    repo = SqlRepository(engine, create_session_factory(engine))
    try:
        # with auto-migrate the schema is created at startup, so only connectivity matters here
        return await database_problem(
            repo, settings, expect_schema=not settings.database.auto_migrate
        )
    finally:
        await repo.close()


def serve(host: str | None, port: int | None) -> int:
    import uvicorn

    settings = get_settings()
    if settings.env == "prod" and not settings.security.api_token:
        print("error: SAA_SECURITY__API_TOKEN must be set when SAA_ENV=prod", file=sys.stderr)
        return 2
    exposure = bind_problem(host or settings.host, settings)
    if exposure:
        print(f"error: {exposure}", file=sys.stderr)
        return 2
    problem = asyncio.run(_preflight(settings))
    if problem:
        print(f"error: {problem}", file=sys.stderr)
        return 2
    uvicorn.run(
        "app.api.main:app_factory", factory=True, host=host or settings.host, port=port or settings.port,
        log_level=settings.log_level.lower(), access_log=False,  # access logs would include query strings
        ws_ping_interval=20, ws_ping_timeout=20,
    )  # fmt: skip
    return 0


def is_loopback(host: str) -> bool:
    import ipaddress

    name = host.strip().strip("[]").lower()
    if name == "localhost":
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False  # any other name could resolve to a network interface


def bind_problem(host: str, settings: Settings) -> str | None:
    """Why the API must not listen on ``host``, or None. Loopback is always fine."""
    if is_loopback(host):
        return None
    if not settings.security.allow_remote_bind:
        return (
            f"refusing to listen on '{host}': this API can control the computer and is meant for "
            "this machine only. Use 127.0.0.1, or set SAA_SECURITY__ALLOW_REMOTE_BIND=true if you "
            "really want it reachable from the network (then put a TLS reverse proxy in front)."
        )
    if not settings.security.api_token:
        return (
            f"refusing to listen on '{host}' without SAA_SECURITY__API_TOKEN: a network-reachable "
            "API needs a token you chose, not a generated one."
        )
    return None


def migrate() -> int:
    from app.container import run_migrations

    run_migrations(get_settings())
    print("migrations applied")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m app",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="run the API and the built UI")
    s.add_argument("--host")
    s.add_argument("--port", type=int)
    sub.add_parser("migrate", help="apply database migrations")
    sub.add_parser("doctor", help="check the environment")
    sub.add_parser("tools", help="list tools")
    sub.add_parser("providers", help="list supported LLM providers")
    lc = sub.add_parser(
        "llm-check", help="run live conformance probes against the configured model"
    )
    lc.add_argument("--vision", action="store_true", help="also test image input")
    args: Any = ap.parse_args(argv)
    commands: dict[str, Callable[[], int]] = {
        "serve": lambda: serve(args.host, args.port),
        "migrate": migrate,
        "doctor": doctor,
        "tools": print_tools,
        "providers": print_providers,
        "llm-check": lambda: llm_check(args.vision),
    }
    return commands[args.cmd]()


if __name__ == "__main__":
    sys.exit(main())
