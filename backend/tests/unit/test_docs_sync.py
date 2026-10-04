"""Generated documentation must match the implementation (regenerate with `python -m app.docs_gen`)."""

from __future__ import annotations

from app.docs_gen import CATALOG, OPENAPI, render_catalog, render_openapi


def _read(p) -> str:  # type: ignore[no-untyped-def]
    return p.read_text(encoding="utf-8").replace("\r\n", "\n")


def test_tool_catalog_is_current() -> None:
    assert _read(CATALOG) == render_catalog(), (
        "docs/tools/catalog.md is stale: run `python -m app.docs_gen`"
    )


def test_openapi_spec_is_current() -> None:
    assert _read(OPENAPI) == render_openapi(), (
        "docs/api/openapi.json is stale: run `python -m app.docs_gen`"
    )
