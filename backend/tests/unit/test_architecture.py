"""Architecture fitness tests: package layering is acyclic and only points downwards."""

from __future__ import annotations

import ast
from collections import defaultdict
from pathlib import Path

APP = Path(__file__).resolve().parents[2] / "app"

# Lower layers must never import higher ones. Packages in the same layer must not form cycles.
LAYERS: list[set[str]] = [
    {"utils"},
    {"models", "config"},
    {"observability", "perception", "recovery", "security", "permissions", "state"},
    {"llm", "database", "search"},
    {"tools"},
    {"verification"},
    {"planner"},
    {"agent"},
    {"orchestration"},
    {"api"},
]
ASSEMBLY = {
    "container",
    "cli",
    "docs_gen",
    "registry",
    "__main__",
}  # composition roots / entrypoints
# The HTTP adapter is the one library layer allowed to reach the composition root (it serves/uses the container).
ALLOWED_ASSEMBLY_IMPORTS = {("api", "container")}


def _package(module: str) -> str:
    parts = module.split(".")
    return parts[1] if len(parts) > 1 else "(root)"


def _graph() -> dict[str, set[str]]:
    edges: dict[str, set[str]] = defaultdict(set)
    for f in APP.rglob("*.py"):
        mod = ".".join(f.relative_to(APP.parent).with_suffix("").parts).removesuffix(".__init__")
        src = _package(mod)
        for node in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
            names: list[str] = []
            if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("app"):
                names = [node.module]
            elif isinstance(node, ast.Import):
                names = [a.name for a in node.names if a.name.startswith("app")]
            for n in names:
                dst = _package(n)
                if dst != src and dst != "(root)":
                    edges[src].add(dst)
    return edges


def _layer(pkg: str) -> int:
    for i, group in enumerate(LAYERS):
        if pkg in group:
            return i
    return 99  # assembly modules sit above everything


def test_every_package_is_assigned_to_a_layer() -> None:
    known = {p for g in LAYERS for p in g} | ASSEMBLY
    actual = {
        _package(".".join(f.relative_to(APP.parent).with_suffix("").parts))
        for f in APP.rglob("*.py")
    } - {"(root)", "__init__"}
    assert actual <= known, f"unassigned packages: {sorted(actual - known)}"


def test_imports_only_point_to_lower_layers() -> None:
    bad = []
    for src, dsts in _graph().items():
        if src in ASSEMBLY:
            continue
        for dst in dsts:
            if dst in ASSEMBLY and (src, dst) in ALLOWED_ASSEMBLY_IMPORTS:
                continue
            if dst in ASSEMBLY:
                bad.append(f"{src} -> {dst} (library code must not import a composition root)")
            elif _layer(dst) > _layer(src):
                bad.append(f"{src} (layer {_layer(src)}) -> {dst} (layer {_layer(dst)})")
    assert not bad, "layering violations:\n  " + "\n  ".join(sorted(bad))


def test_no_package_level_cycles() -> None:
    edges = _graph()
    cycles: set[tuple[str, ...]] = set()

    def dfs(start: str, node: str, path: list[str]) -> None:
        for nxt in edges.get(node, ()):
            if nxt == start and len(path) > 1:
                cycles.add(tuple(sorted(path)))
            elif nxt not in path:
                dfs(start, nxt, [*path, nxt])

    for n in list(edges):
        dfs(n, n, [n])
    assert not cycles, f"import cycles between packages: {sorted(cycles)}"


def test_domain_models_depend_only_on_utils() -> None:
    assert _graph().get("models", set()) <= {"utils"}


def test_the_only_path_to_the_os_is_the_tools_package() -> None:
    """Subprocess/OS-automation primitives must not leak outside tools and the composition roots."""
    risky = ("subprocess", "pyautogui", "pywinauto", "win32gui", "send2trash")
    offenders = []
    for f in APP.rglob("*.py"):
        rel = f.relative_to(APP)
        if rel.parts[0] in {"tools", "cli.py", "container.py"} or rel.parts[0] == "database":
            continue
        text = f.read_text(encoding="utf-8")
        for word in risky:
            if f"import {word}" in text or f"from {word}" in text:
                offenders.append(f"{rel}: {word}")
    assert not offenders, offenders
