"""Windows Job Object around an approved child process.

A job gives three things the process handle alone cannot:

* every process the child starts belongs to the job, so the whole tree ends with it;
* the tree is killed when the job handle is closed, including when THIS process dies, so an
  approved command cannot outlive the agent that is accountable for it;
* optional hard limits on committed memory and on the number of processes.

It is containment of resources and lifetime, not a sandbox: the child still runs with the user's
privileges and can read and write whatever the user can.
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

from app.observability.logging import get_logger

log = get_logger("terminal.job")

_KILL_ON_JOB_CLOSE = 0x00002000
_LIMIT_ACTIVE_PROCESS = 0x00000008
_LIMIT_JOB_MEMORY = 0x00000200
_EXTENDED_LIMIT_INFORMATION = 9
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_TERMINATE = 0x0001


class _BasicLimits(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint64) for name in ("r", "w", "o", "rb", "wb", "ob")]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimits),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class ProcessJob:
    """Holds one job. ``close()`` (or the death of this process) kills everything inside it."""

    def __init__(self, handle: int) -> None:
        self._handle: int | None = handle

    @classmethod
    def attach(
        cls, pid: int, *, max_memory_mb: int = 0, max_processes: int = 0
    ) -> ProcessJob | None:
        """Put a just-started process into a new job. Returns None when that is not possible (not
        Windows, or the system refused): the caller keeps its own timeout and tree kill."""
        if sys.platform != "win32":
            return None
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
        ]  # fmt: skip
        k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        job = k32.CreateJobObjectW(None, None)
        if not job:
            return None
        info = _ExtendedLimits()
        flags = _KILL_ON_JOB_CLOSE
        if max_memory_mb > 0:
            flags |= _LIMIT_JOB_MEMORY
            info.JobMemoryLimit = max_memory_mb * 1024 * 1024
        if max_processes > 0:
            flags |= _LIMIT_ACTIVE_PROCESS
            info.BasicLimitInformation.ActiveProcessLimit = max_processes
        info.BasicLimitInformation.LimitFlags = flags
        proc = None
        try:
            if not k32.SetInformationJobObject(
                job, _EXTENDED_LIMIT_INFORMATION, ctypes.byref(info), ctypes.sizeof(info)
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            proc = k32.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, pid)
            if not proc or not k32.AssignProcessToJobObject(job, proc):
                raise ctypes.WinError(ctypes.get_last_error())
        except OSError as exc:
            log.warning("could not confine the child process in a job", extra={"error": str(exc)})
            k32.CloseHandle(job)
            return None
        finally:
            if proc:
                k32.CloseHandle(proc)
        return cls(job)

    def close(self) -> None:
        """Kill whatever is still running in the job and release it. Safe to call twice."""
        handle, self._handle = self._handle, None
        if handle is None or sys.platform != "win32":
            return
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        k32.TerminateJobObject(handle, 1)
        k32.CloseHandle(handle)
