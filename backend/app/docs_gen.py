"""Generate documentation from the running code, so docs cannot drift from the implementation.

python -m app.docs_gen            # (re)write docs/tools/catalog.md and docs/api/openapi.json
python -m app.docs_gen --check    # exit 1 if the committed docs are out of date (used by tests/CI)
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

from app.config.settings import (
    BrowserSettings,
    FilesystemSettings,
    SecuritySettings,
    Settings,
)
from app.container import build_container
from app.llm.scripted import ScriptedProvider
from app.registry import build_registry
from app.state.memory import InMemoryRepository
from app.tools.desktop.memory import FakeDesktopBackend
from app.tools.vision.service import VisionService

ROOT = Path(__file__).resolve().parents[2]
CATALOG = ROOT / "docs" / "tools" / "catalog.md"
OPENAPI = ROOT / "docs" / "api" / "openapi.json"

CAPABILITY_ORDER = ["filesystem", "terminal", "browser", "api", "desktop", "vision", "verification"]


def _type(schema: dict[str, Any]) -> str:
    if "enum" in schema:
        return " \\| ".join(f"`{e}`" for e in schema["enum"])
    if "anyOf" in schema:
        return " \\| ".join(t for t in (_type(s) for s in schema["anyOf"]) if t != "null") or "any"
    t = schema.get("type", "any")
    return f"{_type(schema.get('items', {}))}[]" if t == "array" else str(t)


def render_catalog() -> str:
    registry = build_registry(include_browser=True, include_desktop=True, include_vision=True)
    specs = sorted(
        registry.specs(), key=lambda s: (CAPABILITY_ORDER.index(s.capability.value), s.name)
    )
    lines = [
        "# Tool catalogue",
        "",
        "> Generated from the tool registry by `python -m app.docs_gen`. Do not edit by hand.",
        "",
        "Every operation the agent can perform is one of these typed tools; the model never gets a shell or raw OS access.",
        "*Base risk* is the tool's default; each call is re-assessed (a delete of a non-empty folder is `critical`, a command that "
        "runs project code is `high`, ...). `approval: policy` means the permission engine decides from the assessed risk; `always` always asks.",
        "",
    ]
    current = ""
    for s in specs:
        if s.capability.value != current:
            current = s.capability.value
            lines += [
                f"## {current.capitalize()}",
                "",
                "| Tool | Base risk | Approval | Timeout | Read-only | Idempotent | Untrusted output |",
                "|---|---|---|---|---|---|---|",
            ]
            lines_header_index = len(lines)
            _ = lines_header_index
        lines.append(
            f"| `{s.name}` | {s.risk.label} | {s.approval.value} | {s.timeout_s:g}s | {'yes' if s.read_only else 'no'} | "
            f"{'yes' if s.idempotent else 'no'} | {'yes' if s.returns_external_content else 'no'} |"
        )
        nxt = [x for x in specs if x.capability == s.capability]
        if s is nxt[-1]:
            lines.append("")
            for t in nxt:
                props = t.input_schema.get("properties", {})
                req = set(t.input_schema.get("required", []))
                lines += [f"### `{t.name}`", "", t.description, ""]
                if props:
                    lines += ["| Parameter | Type | Required | Description |", "|---|---|---|---|"]
                    for name, p in props.items():
                        lines.append(
                            f"| `{name}` | {_type(p)} | {'yes' if name in req else 'no'} | {str(p.get('description', '')).replace('|', '/')} |"
                        )
                    lines.append("")
                else:
                    lines += ["_No parameters._", ""]
                if t.retry.max_attempts > 1:
                    lines += [
                        f"Retries: up to {t.retry.max_attempts} attempts on `{', '.join(t.retry.retry_on) or 'retryable errors'}`.",
                        "",
                    ]
    return "\n".join(lines).rstrip() + "\n"


def render_openapi() -> str:
    with tempfile.TemporaryDirectory(prefix="saa-docs-") as tmp:
        root = Path(tmp)
        settings = Settings(
            env="test", log_json=False, log_level="WARNING", serve_frontend=False,
            filesystem=FilesystemSettings(allowed_roots=[root], workspace=root / "w"),
            browser=BrowserSettings(), security=SecuritySettings(api_token="docs"),  # noqa: S106 - placeholder for spec generation
        )  # fmt: skip
        c = build_container(
            settings,
            repo=InMemoryRepository(),
            llm=ScriptedProvider(),
            enable_browser=False,
            enable_desktop=False,
            desktop_backend=FakeDesktopBackend(),
        )
        from app.api.main import create_app

        spec = create_app(c).openapi()
        return json.dumps(spec, indent=2, sort_keys=True) + "\n"


_ = VisionService


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    targets = {CATALOG: render_catalog(), OPENAPI: render_openapi()}
    stale = []
    for path, content in targets.items():
        current = path.read_text(encoding="utf-8") if path.exists() else ""
        if current.replace("\r\n", "\n") != content:
            stale.append(path)
            if not args.check:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8", newline="\n")
                print(f"wrote {path.relative_to(ROOT)}")
    if args.check and stale:
        print(
            "out of date: "
            + ", ".join(str(p.relative_to(ROOT)) for p in stale)
            + "\nrun: python -m app.docs_gen",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
