"""The documented .env.example must stay in sync with the real settings model."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from app.config.settings import Settings
from pydantic import BaseModel

EXAMPLE = Path(__file__).resolve().parents[3] / ".env.example"
LINE = re.compile(r"^#?\s*(SAA_[A-Z0-9_]+)=(.*?)(\s+#.*)?$")


def entries() -> list[tuple[str, str, bool]]:
    out = []
    for raw in EXAMPLE.read_text(encoding="utf-8").splitlines():
        m = LINE.match(raw.strip())
        if m:
            out.append((m.group(1), m.group(2).strip(), raw.strip().startswith("#")))
    return out


def resolve_field(path: list[str]) -> bool:
    model: type[BaseModel] = Settings
    for i, part in enumerate(path):
        field = model.model_fields.get(part.lower())
        if field is None:
            return False
        ann = field.annotation
        if i < len(path) - 1:
            if not (isinstance(ann, type) and issubclass(ann, BaseModel)):
                return False
            model = ann
    return True


def test_example_exists_and_is_nonempty() -> None:
    assert len(entries()) > 30


@pytest.mark.parametrize(("key", "_v", "_c"), entries())
def test_every_documented_variable_is_a_real_setting(key: str, _v: str, _c: bool) -> None:
    path = key.removeprefix("SAA_").lower().split("__")
    assert resolve_field(path), f"{key} is documented but is not a setting"


def test_example_values_load_without_error(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in [k for k in list(__import__("os").environ) if k.startswith("SAA_")]:
        monkeypatch.delenv(k)
    for key, value, commented in entries():
        if not commented:
            monkeypatch.setenv(key, value)
    s = Settings()
    assert (
        s.llm.provider == "anthropic" and s.security.allowed_origins and s.limits.max_actions == 200
    )
    assert s.terminal.allow_shell_interpreters is False and s.browser.allow_private_network is False
    assert s.filesystem.workspace.name != "", (
        "an empty path must never silently become the current directory"
    )
