"""Process runner: direct argv execution (no shell), timeout, output cap, tree kill."""

from __future__ import annotations

import asyncio
import contextlib
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from app.tools.terminal.job import ProcessJob


@dataclass
class ProcessResult:
    exit_code: int | None
    stdout: str
    stderr: str
    duration_ms: float
    timed_out: bool
    truncated: bool


def kill_process_tree(pid: int) -> None:
    if sys.platform == "win32":
        with contextlib.suppress(Exception):
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True, timeout=10, check=False,
            )  # fmt: skip
    else:  # pragma: no cover - POSIX path
        import signal

        with contextlib.suppress(Exception):
            os.killpg(os.getpgid(pid), signal.SIGKILL)


async def _drain(stream: asyncio.StreamReader | None, cap: int) -> tuple[bytes, bool]:
    if stream is None:
        return b"", False
    buf = bytearray()
    truncated = False
    while True:
        chunk = await stream.read(8192)
        if not chunk:
            break
        if len(buf) < cap:
            buf.extend(chunk[: cap - len(buf)])
            if len(buf) >= cap:
                truncated = True
        else:
            truncated = True  # keep draining so the child never blocks on a full pipe
    return bytes(buf), truncated


async def _exited(proc: asyncio.subprocess.Process) -> None:
    """Return when the command's own process has exited.

    ``proc.wait()`` is not used for this: asyncio completes it only after the output pipes are
    closed, and a child the command left running keeps them open, so a command that had finished
    was reported only when its timeout expired.
    """
    while proc.returncode is None:  # noqa: ASYNC110 - asyncio offers no exit event without the pipes
        await asyncio.sleep(0.05)


_DRAIN_GRACE_S = 5.0  # how long to wait for end-of-output after the command has exited


async def run_process(
    argv: list[str], *, cwd: Path, env: dict[str, str], timeout_s: float, max_output_bytes: int,
    max_memory_mb: int = 0, max_processes: int = 0,
) -> ProcessResult:  # fmt: skip
    started = time.perf_counter()
    kwargs: dict[str, object] = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    else:  # pragma: no cover
        kwargs["start_new_session"] = True
    proc = await asyncio.create_subprocess_exec(
        *argv, cwd=str(cwd), env=env,
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        **kwargs,  # type: ignore[arg-type]
    )  # fmt: skip
    # The job ties the whole process tree to this call: it is killed when the call ends for any
    # reason (timeout, STOP, an error here) and when the agent process itself dies.
    job = ProcessJob.attach(proc.pid, max_memory_mb=max_memory_mb, max_processes=max_processes)
    timed_out = False
    try:
        out_task = asyncio.ensure_future(_drain(proc.stdout, max_output_bytes))
        err_task = asyncio.ensure_future(_drain(proc.stderr, max_output_bytes))
        try:
            await asyncio.wait_for(_exited(proc), timeout=timeout_s)
        except TimeoutError:
            timed_out = True
            kill_process_tree(proc.pid)
            with contextlib.suppress(Exception):
                proc.kill()
            await proc.wait()
        # The command itself has ended. A child it left running still holds the output pipes open
        # and would keep this call waiting for end-of-output indefinitely: end the tree first.
        # What was already written stays readable.
        if job is not None:
            job.close()
            job = None
        try:
            (out, t1), (err, t2) = await asyncio.wait_for(
                asyncio.gather(out_task, err_task), timeout=_DRAIN_GRACE_S
            )
        except TimeoutError:  # no job (not Windows, or refused) and an orphan keeps a pipe open
            (out, t1), (err, t2) = (b"", True), (b"", True)
    except asyncio.CancelledError:
        kill_process_tree(proc.pid)
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        raise
    finally:
        if proc.returncode is None:
            kill_process_tree(proc.pid)
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
        if job is not None:
            job.close()  # also ends children the command left running in the background
    return ProcessResult(
        exit_code=None if timed_out else proc.returncode,
        stdout=out.decode("utf-8", errors="replace"),
        stderr=err.decode("utf-8", errors="replace"),
        duration_ms=(time.perf_counter() - started) * 1000,
        timed_out=timed_out,
        truncated=t1 or t2,
    )
