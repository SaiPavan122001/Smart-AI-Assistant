"""NEXT-C: an approved command and everything it starts live and die with the call that ran it."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from app.security.commands import build_environment
from app.tools.terminal.job import ProcessJob
from app.tools.terminal.runner import run_process

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Objects")

# The interpreter itself, not the virtualenv's launcher: the launcher is a second process that starts
# the real one and waits, so it would be counted (and limited) as part of the command under test.
PYTHON = getattr(sys, "_base_executable", sys.executable)
HEARTBEAT = (
    "import sys, time\nwhile True:\n    open(sys.argv[1], 'a').write('x')\n    time.sleep(0.1)\n"
)


async def run(code: str, tmp_path: Path, *args: str, timeout_s: float = 20, **limits: int):  # type: ignore[no-untyped-def]
    script = tmp_path / "main.py"
    script.write_text(code)
    return await run_process(
        [PYTHON, str(script), *args], cwd=tmp_path, env=build_environment(),
        timeout_s=timeout_s, max_output_bytes=10_000, **limits,
    )  # fmt: skip


def still_beating(beat: Path) -> bool:
    before = beat.stat().st_size if beat.exists() else 0
    time.sleep(0.6)
    return (beat.stat().st_size if beat.exists() else 0) > before


async def test_a_background_process_left_by_a_command_does_not_outlive_it(tmp_path: Path) -> None:
    """The command returns at once but leaves a child running. It must not keep running unattended."""
    (tmp_path / "beat.py").write_text(HEARTBEAT)
    beat = tmp_path / "beat.txt"
    launcher = (
        "import subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, 'beat.py', sys.argv[1]])\n"
        "time.sleep(0.8)\n"
    )
    result = await run(launcher, tmp_path, str(beat))
    assert result.exit_code == 0 and beat.exists(), "the child did start"
    assert not still_beating(beat), "the left-behind child was ended with the command"


async def test_a_timed_out_command_takes_its_children_with_it(tmp_path: Path) -> None:
    (tmp_path / "beat.py").write_text(HEARTBEAT)
    beat = tmp_path / "beat.txt"
    launcher = (
        "import subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, 'beat.py', sys.argv[1]])\n"
        "time.sleep(60)\n"
    )
    result = await run(launcher, tmp_path, str(beat), timeout_s=1.5)
    assert result.timed_out and not still_beating(beat)


async def test_a_memory_limit_stops_a_runaway_command(tmp_path: Path) -> None:
    hog = "data = bytearray(400 * 1024 * 1024)\nprint('allocated')\n"
    capped = await run(hog, tmp_path, max_memory_mb=96)
    assert capped.exit_code not in (0, None) and "allocated" not in capped.stdout
    modest = await run("print('fine')\n", tmp_path, max_memory_mb=96)
    assert modest.exit_code == 0 and "fine" in modest.stdout


async def test_a_process_limit_stops_a_command_from_fanning_out(tmp_path: Path) -> None:
    spawn = (
        "import subprocess, sys\n"
        "try:\n"
        "    subprocess.run([sys.executable, '-c', 'print(1)'], check=True)\n"
        "    print('spawned')\n"
        "except OSError as exc:\n"
        "    print('refused', type(exc).__name__)\n"
    )
    limited = await run(spawn, tmp_path, max_processes=1)
    assert "spawned" not in limited.stdout and "refused" in limited.stdout
    free = await run(spawn, tmp_path)
    assert "spawned" in free.stdout


def test_closing_the_job_ends_the_process_and_is_idempotent(tmp_path: Path) -> None:
    proc = subprocess.Popen(  # noqa: S603
        [sys.executable, "-c", "import time; time.sleep(60)"],
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    try:
        job = ProcessJob.attach(proc.pid)
        assert job is not None
        assert proc.poll() is None
        job.close()
        assert proc.wait(timeout=10) != 0
        job.close()  # second close: nothing happens
    finally:
        if proc.poll() is None:
            proc.kill()


def test_a_process_that_no_longer_exists_cannot_be_confined() -> None:
    assert ProcessJob.attach(os.getpid() + 10_000_019) is None


async def test_cancelling_the_call_kills_the_tree(tmp_path: Path) -> None:
    (tmp_path / "beat.py").write_text(HEARTBEAT)
    beat = tmp_path / "beat.txt"
    launcher = (
        "import subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, 'beat.py', sys.argv[1]])\n"
        "time.sleep(60)\n"
    )
    task = asyncio.create_task(run(launcher, tmp_path, str(beat)))
    for _ in range(100):
        if beat.exists():
            break
        await asyncio.sleep(0.1)
    task.cancel()  # what STOP does to a running tool
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not still_beating(beat)
