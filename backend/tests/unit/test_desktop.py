"""Desktop tools against the in-memory test adapter (UIA itself is covered in test_uia_windows.py)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from app.models.tool import ToolCall
from app.tools.desktop import keys as keymod
from app.tools.desktop.memory import FakeDesktopBackend, FakeElement, FakeWindow
from app.tools.desktop.models import UIElement
from app.tools.desktop.service import DesktopService, score_ui
from app.utils.errors import ErrorCode, PolicyViolation

from tests.helpers import Env, make_env


@pytest.fixture
def desk(tmp_path: Path) -> tuple[Env, FakeDesktopBackend]:
    env = make_env(tmp_path, include_desktop=True)
    backend = FakeDesktopBackend()
    svc = DesktopService(backend, env.settings.desktop)
    env.services.desktop = svc
    return env, backend


def vscode(backend: FakeDesktopBackend) -> FakeWindow:
    def run_clicked(b: FakeDesktopBackend, el: FakeElement) -> None:
        b.windows[0].elements.append(FakeElement("text", "Output: OK"))

    return backend.add_window(
        "main.py - expense_tracker - Visual Studio Code", "Code.exe", active=True,
        elements=[
            FakeElement("button", "Run", automation_id="workbench.action.debug.run", on_click=run_clicked),
            FakeElement("button", "Explorer", automation_id="explorer"),
            FakeElement("textbox", "Search", automation_id="search.input", value=""),
            FakeElement("document", "Editor", value="print('hi')"),
            FakeElement("button", "Delete all data"),
            FakeElement("textbox", "Password", sensitive=True, value="hunter2"),
        ],
    )  # fmt: skip


async def run(env: Env, tool: str, **args: Any):  # type: ignore[no-untyped-def]
    return await env.router.execute(ToolCall(tool=tool, args=args, rationale="test"), env.ctx())


async def test_windows_and_structured_ui_tree(desk: tuple[Env, FakeDesktopBackend]) -> None:
    env, be = desk
    vscode(be)
    be.add_window("Notes - Notepad", "notepad.exe")
    r = await run(env, "desktop_windows")
    assert (
        r.ok
        and {w["process"] for w in r.data["windows"]} == {"Code.exe", "notepad.exe"}
        and r.perception_source == "uia"
    )
    assert (await run(env, "desktop_active_window")).data["window"]["process"] == "Code.exe"
    tree = await run(env, "desktop_read", mode="tree", window_title="Visual Studio Code")
    assert tree.ok and tree.data["quality"] == "good"
    assert any(
        'button "Run"' in line and "id=workbench.action.debug.run" in line
        for line in tree.data["tree"]
    )


async def test_find_by_automation_id_is_preferred_and_ambiguity_is_reported(
    desk: tuple[Env, FakeDesktopBackend],
) -> None:
    env, be = desk
    w = vscode(be)
    w.elements.append(FakeElement("button", "Run", automation_id="other.run"))
    r = await run(env, "desktop_find_element", automation_id="workbench.action.debug.run")
    assert (
        r.ok
        and r.data["matches"][0]["automation_id"] == "workbench.action.debug.run"
        and not r.data["ambiguous"]
    )
    r = await run(env, "desktop_find_element", query="Run", role="button")
    assert r.ok and r.data["ambiguous"]
    click = await run(env, "desktop_click", query="Run")
    assert (
        not click.ok
        and click.error.code == ErrorCode.AMBIGUOUS_TARGET
        and click.error.details["candidates"]
    )  # type: ignore[union-attr]
    ok = await run(env, "desktop_click", automation_id="workbench.action.debug.run")
    assert ok.ok and be.clicks == ["Run"]


async def test_click_and_verify_state_change(desk: tuple[Env, FakeDesktopBackend]) -> None:
    env, be = desk
    vscode(be)
    r = await run(
        env, "desktop_click", query="Run", role="button", window_title="Visual Studio Code"
    )
    assert r.ok and "invoke" in r.summary
    v = await run(
        env, "verify_desktop_state", window_title="Visual Studio Code", element_query="Output: OK"
    )
    assert v.ok and v.data["passed"] is True


async def test_type_into_textbox_sets_value_and_masks_sensitive_fields(
    desk: tuple[Env, FakeDesktopBackend],
) -> None:
    env, be = desk
    w = vscode(be)
    r = await run(env, "desktop_type", query="Search", role="textbox", text="spring boot")
    assert r.ok and w.elements[2].value == "spring boot"
    not_text = await run(env, "desktop_type", automation_id="explorer", text="x")
    assert not not_text.ok and "not a text control" in not_text.error.message  # type: ignore[union-attr]
    el = (await run(env, "desktop_find_element", query="Password")).data["matches"][0]
    read = await run(env, "desktop_read", mode="element", ref=el["ref"])
    assert "hunter2" not in read.model_dump_json(), "sensitive control values are never read back"


async def test_sensitive_button_requires_approval(desk: tuple[Env, FakeDesktopBackend]) -> None:
    env, be = desk
    vscode(be)
    tool = env.router.registry.get("desktop_click")
    args = tool.Input(query="Delete all data")  # type: ignore[union-attr]
    a = await tool.assess(args, env.ctx())  # type: ignore[union-attr,misc]
    assert a.risk.label == "high" and a.category == "sensitive_ui_action"


@pytest.mark.parametrize(
    "proc", ["cmd.exe", "powershell.exe", "WindowsTerminal.exe", "pwsh.exe", "bash.exe"]
)
async def test_terminals_cannot_receive_input(
    desk: tuple[Env, FakeDesktopBackend], proc: str
) -> None:
    env, be = desk
    be.add_window(
        "Terminal",
        proc,
        elements=[FakeElement("textbox", "Input"), FakeElement("button", "OK")],
        active=True,
    )
    for tool, args in (
        ("desktop_type", {"query": "Input", "text": "del /s *"}),
        ("desktop_key", {"keys": "Enter"}),
        ("desktop_click", {"query": "OK"}),
        ("desktop_scroll", {}),
    ):
        r = await run(env, tool, **args)
        assert (
            not r.ok
            and r.error.code == ErrorCode.POLICY_DENIED
            and "terminal" in r.error.message.lower()
        ), (proc, tool)  # type: ignore[union-attr]
    assert be.keys_sent == [] and be.typed == []


async def test_protected_windows_are_invisible_and_untouchable(
    desk: tuple[Env, FakeDesktopBackend],
) -> None:
    env, be = desk
    be.add_window(
        "Windows Security",
        "SecHealthUI.exe",
        elements=[FakeElement("button", "Turn off")],
        active=True,
    )
    be.add_window(
        "KeePassXC - vault.kdbx",
        "KeePassXC.exe",
        elements=[FakeElement("textbox", "Master password")],
    )
    listing = await run(env, "desktop_windows")
    assert listing.data["windows"] == []
    assert not (
        await run(env, "desktop_click", query="Turn off", window_title="Windows Security")
    ).ok
    assert not (await run(env, "desktop_read", mode="tree", window_title="KeePassXC")).ok


async def test_key_grammar_and_risky_combos(desk: tuple[Env, FakeDesktopBackend]) -> None:
    env, be = desk
    vscode(be)
    assert (await run(env, "desktop_key", keys="Ctrl+S")).ok and be.keys_sent == [["ctrl", "s"]]
    for bad in (
        "Win+R",
        "Windows+E",
        "Ctrl+Win+D",
        "Ctrl+Shift+Alt+Super+X",
        "Banana",
        "",
        "Ctrl+",
    ):
        r = await run(env, "desktop_key", keys=bad)
        assert not r.ok, bad
    tool = env.router.registry.get("desktop_key")
    risky = await tool.assess(tool.Input(keys="Alt+F4"), env.ctx())  # type: ignore[union-attr,misc]
    assert risky.risk.label == "high"


def test_key_parsing_and_pywinauto_translation() -> None:
    assert keymod.parse_combo("Ctrl+Shift+P") == ["ctrl", "shift", "p"]
    assert (
        keymod.to_pywinauto(["ctrl", "s"]) == "^s"
        and keymod.to_pywinauto(["alt", "f4"]) == "%{F4}"
        and keymod.to_pywinauto(["enter"]) == "{ENTER}"
    )
    assert keymod.escape_literal("a+b{c}") == "a{+}b{{}c{}}"
    with pytest.raises(PolicyViolation):
        keymod.parse_combo("Win+R")


async def test_launch_is_allowlisted_and_paths_are_policed(
    desk: tuple[Env, FakeDesktopBackend],
) -> None:
    env, be = desk
    be.launch_handlers["code"] = lambda b: FakeWindow(
        handle=9, title="expense_tracker - Visual Studio Code", process="Code.exe"
    )
    proj = env.workspace / "expense_tracker"
    proj.mkdir()
    r = await run(env, "desktop_launch", app="vscode", args=[str(proj)])
    assert r.ok and r.data["window"]["title"].startswith("expense_tracker")
    assert be.launched[0][1] == [str(proj)]
    for bad in (
        {"app": "regedit"},
        {"app": "vscode", "args": ["C:/Windows"]},
        {"app": "vscode", "args": ["--install-extension", "evil.ext"]},
        {"app": "powershell"},
    ):
        r = await run(env, "desktop_launch", **bad)
        assert not r.ok and r.error.code == ErrorCode.POLICY_DENIED, bad  # type: ignore[union-attr]


async def test_poorly_instrumented_app_degrades_gracefully_with_vision_hint(
    desk: tuple[Env, FakeDesktopBackend],
) -> None:
    env, be = desk
    be.add_window("LegacyApp", "legacy.exe", elements=[FakeElement("pane", "canvas")], active=True)
    r = await run(env, "desktop_find_element", query="Save")
    assert not r.ok and r.error.code == ErrorCode.ELEMENT_NOT_FOUND  # type: ignore[union-attr]
    assert r.error.details["quality"] == "poor" and "vision" in r.error.details["fallback"]  # type: ignore[union-attr]


async def test_stale_refs_are_re_resolved_by_identity(desk: tuple[Env, FakeDesktopBackend]) -> None:
    env, be = desk
    vscode(be)
    ref = (await run(env, "desktop_find_element", automation_id="explorer")).data["matches"][0][
        "ref"
    ]
    be._refs.clear()  # noqa: SLF001 - the app re-rendered: every old ref is dead
    r = await run(env, "desktop_click", ref=ref)
    assert r.ok and be.clicks[-1] == "Explorer"


def test_scoring_prefers_automation_id_then_exact_name() -> None:
    els = [
        UIElement(ref="a", role="button", name="Run all"),
        UIElement(ref="b", role="button", name="Run", automation_id="run"),
        UIElement(ref="c", role="textbox", name="Run"),
    ]
    ranked = score_ui(els, "Run", "button", None)
    assert ranked[0][1].ref == "b" and [e.ref for _, e in score_ui(els, None, None, "run")] == ["b"]


def test_default_tree_depth_covers_uwp_apps() -> None:
    """Regression: Calculator's controls sit ~10 levels deep; depth 6 made the real app look empty."""
    from app.config.settings import DesktopSettings

    assert DesktopSettings().max_tree_depth >= 12


def test_generic_text_entry_descriptors_resolve_by_role_but_never_guess_between_several() -> None:
    """Regression (live run C): 'text area' / 'Notepad text area' could not find Notepad's single 'Text editor' control."""
    from app.tools.desktop.service import is_text_entry_descriptor

    notepad = [
        UIElement(ref="m", role="menuitem", name="Edit"),
        UIElement(ref="d", role="document", name="Text editor"),
        UIElement(ref="b", role="button", name="Add New Tab"),
    ]
    for q in ("text area", "Notepad text area", "text box", "the editor", "input field"):
        r = score_ui(notepad, q, None, None, "Untitled - Notepad")
        assert r and r[0][1].ref == "d" and r[0][0] >= 50, q
    assert [e.ref for _, e in score_ui(notepad, "text area", "document", None, "Notepad")] == ["d"]
    # several text boxes: equal scores -> the service reports ambiguity instead of guessing
    form = [
        UIElement(ref="a", role="textbox", name="Name"),
        UIElement(ref="b", role="textbox", name="Email"),
    ]
    top = score_ui(form, "text field", None, None, "Form")
    assert len(top) == 2 and top[0][0] == top[1][0]
    # a real name still wins over the generic role match
    named = [
        UIElement(ref="a", role="textbox", name="Input"),
        UIElement(ref="b", role="textbox", name="Other"),
    ]
    assert score_ui(named, "input", None, None, "")[0][1].ref == "a"
    # names and specific targets are not descriptors
    assert not is_text_entry_descriptor("Save button") and not is_text_entry_descriptor(
        "Search box"
    )
    assert not score_ui(notepad, "Submit", None, None, "Notepad")


def test_text_entry_descriptor_falls_back_when_the_requested_role_is_absent() -> None:
    notepad = [UIElement(ref="d", role="document", name="Text editor")]
    assert [e.ref for _, e in score_ui(notepad, "text box", "textbox", None, "Notepad")] == ["d"]


def test_symbol_queries_match_spoken_names_and_short_queries_never_match_inside_words() -> None:
    """Regression (live Calculator probe): '2' found nothing; 'x' matched Maximize/Expression/Six."""
    calc = [
        UIElement(ref="2", role="button", name="Two", automation_id="num2Button"),
        UIElement(ref="m", role="button", name="Multiply by", automation_id="multiplyButton"),
        UIElement(ref="e", role="button", name="Equals"),
        UIElement(ref="z", role="button", name="Maximize Calculator"),
        UIElement(ref="x", role="text", name="Expression is"),
        UIElement(ref="6", role="button", name="Six"),
    ]
    for q, ref in (("2", "2"), ("*", "m"), ("×", "m"), ("x", "m"), ("=", "e"), ("multiply", "m")):
        top = score_ui(calc, q, None, None, "Calculator")
        assert top and top[0][1].ref == ref and top[0][0] >= 50, q
    assert [e.ref for _, e in score_ui(calc, "x", None, None, "Calculator")] == [
        "m"
    ]  # not Maximize/Expression/Six
    assert score_ui(calc, "ma", None, None, "Calculator") == []  # 2 chars: whole words only
    assert not score_ui(
        calc, "9", None, None, "Calculator"
    )  # no 'Nine' on this keypad -> not found, not guessed
