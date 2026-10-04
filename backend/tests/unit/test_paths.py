from __future__ import annotations

import os
from pathlib import Path

import pytest
from app.security.paths import PathPolicy
from app.utils.errors import PathViolation


@pytest.fixture
def policy(tmp_path: Path) -> PathPolicy:
    (tmp_path / "root").mkdir()
    (tmp_path / "ro").mkdir()
    (tmp_path / "root" / "secret_dir").mkdir()
    return PathPolicy(
        allowed_roots=[tmp_path / "root"],
        read_only_roots=[tmp_path / "ro"],
        blocked_paths=[tmp_path / "root" / "secret_dir"],
        workspace=tmp_path / "root",
        blocked_write_extensions=(".exe", ".bat"),
    )


def test_resolves_inside_root(policy: PathPolicy, tmp_path: Path) -> None:
    rp = policy.resolve("notes/a.txt", write=True)
    assert rp.path == (tmp_path / "root" / "notes" / "a.txt").resolve()


@pytest.mark.parametrize(
    "bad",
    [
        "../outside.txt",
        "../../etc/passwd",
        "sub/../../escape",
        "C:/Windows/System32/cmd.exe",
        "\\\\server\\share\\x",
        "\\\\?\\C:\\x",
        "%USERPROFILE%/x",
        "$env:USERPROFILE",
        "file.txt:hidden",
        "",
        "con",
        "nul.txt",
    ],
)
def test_rejects_unsafe_paths(policy: PathPolicy, bad: str) -> None:
    with pytest.raises(PathViolation):
        policy.resolve(bad)


def test_blocked_subpath(policy: PathPolicy) -> None:
    with pytest.raises(PathViolation):
        policy.resolve("secret_dir/x.txt")


def test_read_only_root(policy: PathPolicy, tmp_path: Path) -> None:
    target = str(tmp_path / "ro" / "a.txt")
    policy.resolve(target)  # reading is fine
    with pytest.raises(PathViolation):
        policy.resolve(target, write=True)


def test_blocked_extension_on_write(policy: PathPolicy) -> None:
    with pytest.raises(PathViolation):
        policy.resolve("run.exe", write=True)
    policy.resolve("run.exe")  # read path is not extension-restricted


def test_sensitive_names_blocked(policy: PathPolicy) -> None:
    for name in (".env", "id_rsa", "server.pem", "credentials.json"):
        with pytest.raises(PathViolation):
            policy.resolve(name)


def test_alias(policy: PathPolicy) -> None:
    rp = policy.resolve("@workspace/x.txt", write=True)
    assert rp.path.name == "x.txt"
    with pytest.raises(PathViolation):
        policy.resolve("@nonsense/x")


def test_root_itself_can_be_forbidden(policy: PathPolicy, tmp_path: Path) -> None:
    with pytest.raises(PathViolation):
        policy.resolve(str(tmp_path / "root"), allow_root_itself=False)


@pytest.mark.skipif(os.name != "nt", reason="junction test is Windows specific")
def test_junction_escape_is_blocked(policy: PathPolicy, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "target.txt").write_text("x")
    link = tmp_path / "root" / "link"
    import subprocess

    r = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(outside)], capture_output=True, check=False
    )
    if r.returncode != 0:
        pytest.skip("cannot create junction")
    with pytest.raises(PathViolation):
        policy.resolve("link/target.txt")


def test_symlink_escape_is_blocked(policy: PathPolicy, tmp_path: Path) -> None:
    outside = tmp_path / "outside2"
    outside.mkdir()
    link = tmp_path / "root" / "slink"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    with pytest.raises(PathViolation):
        policy.resolve("slink/new.txt", write=True)
