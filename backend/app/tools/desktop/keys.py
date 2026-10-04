"""Key-combination grammar for desktop_key: validated, never raw key strings."""

from __future__ import annotations

import re

from app.utils.errors import PolicyViolation

_MODS = {"ctrl": "ctrl", "control": "ctrl", "shift": "shift", "alt": "alt"}
_NAMED = {
    "enter": "enter", "return": "enter", "esc": "esc", "escape": "esc", "tab": "tab", "space": "space",
    "backspace": "backspace", "delete": "delete", "del": "delete", "home": "home", "end": "end",
    "pageup": "pageup", "pagedown": "pagedown", "up": "up", "down": "down", "left": "left", "right": "right",
    **{f"f{i}": f"f{i}" for i in range(1, 13)},
}  # fmt: skip
_BLOCKED_TOKENS = {"win", "windows", "cmd", "meta", "super", "lwin", "rwin"}
_RISKY = {
    ("alt", "f4"), ("ctrl", "shift", "delete"), ("ctrl", "q"), ("ctrl", "shift", "q"), ("ctrl", "alt", "delete"),
    ("ctrl", "shift", "esc"),
}  # fmt: skip
_PYAUTO = {"esc": "esc", "pageup": "pageup", "pagedown": "pagedown"}


def parse_combo(combo: str) -> list[str]:
    """'Ctrl+Shift+P' -> ['ctrl', 'shift', 'p']. Raises PolicyViolation for unsafe/unknown keys."""
    parts = [p.strip().lower() for p in re.split(r"\+(?=.)", combo.strip()) if p.strip()]
    if not parts:
        raise PolicyViolation("Empty key combination")
    if len(parts) > 4:
        raise PolicyViolation("Key combination is too long")
    keys: list[str] = []
    for p in parts[:-1]:
        if p in _BLOCKED_TOKENS:
            raise PolicyViolation("The Windows key is not allowed (it can open Run/system dialogs)")
        if p not in _MODS:
            raise PolicyViolation(f"'{p}' is not a modifier (use Ctrl/Shift/Alt)")
        keys.append(_MODS[p])
    last = parts[-1]
    if last in _BLOCKED_TOKENS:
        raise PolicyViolation("The Windows key is not allowed")
    if last in _NAMED:
        keys.append(_NAMED[last])
    elif re.fullmatch(r"[a-z0-9]", last) or last in {
        "`",
        "-",
        "=",
        "[",
        "]",
        ";",
        "'",
        ",",
        ".",
        "/",
        "\\",
    }:
        keys.append(last)
    else:
        raise PolicyViolation(f"Unknown key '{last}'")
    return [_PYAUTO.get(k, k) for k in keys]


def is_risky(keys: list[str]) -> bool:
    return tuple(keys) in _RISKY


def to_pywinauto(keys: list[str]) -> str:
    """Convert to pywinauto send_keys syntax."""
    mod = {"ctrl": "^", "shift": "+", "alt": "%"}
    prefix = "".join(mod[k] for k in keys[:-1])
    last = keys[-1]
    named = {
        "enter": "{ENTER}", "esc": "{ESC}", "tab": "{TAB}", "space": "{SPACE}", "backspace": "{BACKSPACE}",
        "delete": "{DELETE}", "home": "{HOME}", "end": "{END}", "pageup": "{PGUP}", "pagedown": "{PGDN}",
        "up": "{UP}", "down": "{DOWN}", "left": "{LEFT}", "right": "{RIGHT}",
    }  # fmt: skip
    if last in named:
        body = named[last]
    elif re.fullmatch(r"f(1[0-2]|[1-9])", last):
        body = "{" + last.upper() + "}"
    elif last in "+^%~(){}[]":
        body = "{" + last + "}"
    else:
        body = last
    return prefix + body


def escape_literal(text: str) -> str:
    """Escape pywinauto special characters so text is typed literally."""
    return "".join("{" + c + "}" if c in "+^%~(){}[]" else c for c in text)
