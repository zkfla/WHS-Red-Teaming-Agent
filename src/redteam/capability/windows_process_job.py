"""Windows process-family ownership for bounded image preparation.

The launcher joins a named Job Object before it creates the command. The
calling process retains the only job handle, so descendants remain owned
after the command/launcher exits. Closing that handle kills surviving
members without touching the Docker service.
"""

from __future__ import annotations

import ctypes
import subprocess
import sys
import uuid
from typing import Any


class _BasicLimits(ctypes.Structure):
    _fields_ = [
        ("process_time", ctypes.c_int64),
        ("job_time", ctypes.c_int64),
        ("flags", ctypes.c_uint32),
        ("min_working_set", ctypes.c_size_t),
        ("max_working_set", ctypes.c_size_t),
        ("process_limit", ctypes.c_uint32),
        ("affinity", ctypes.c_size_t),
        ("priority", ctypes.c_uint32),
        ("scheduling", ctypes.c_uint32),
    ]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("basic", _BasicLimits),
        ("io_counters", ctypes.c_uint64 * 6),
        ("process_memory", ctypes.c_size_t),
        ("job_memory", ctypes.c_size_t),
        ("peak_process_memory", ctypes.c_size_t),
        ("peak_job_memory", ctypes.c_size_t),
    ]


def _kernel() -> Any:
    windows_ctypes: Any = ctypes
    kernel = windows_ctypes.WinDLL("kernel32", use_last_error=True)
    signatures = {
        "CreateJobObjectW": ([ctypes.c_void_p, ctypes.c_wchar_p], ctypes.c_void_p),
        "SetInformationJobObject": (
            [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32],
            ctypes.c_int,
        ),
        "OpenJobObjectW": ([ctypes.c_uint32, ctypes.c_int, ctypes.c_wchar_p], ctypes.c_void_p),
        "AssignProcessToJobObject": ([ctypes.c_void_p, ctypes.c_void_p], ctypes.c_int),
        "GetCurrentProcess": ([], ctypes.c_void_p),
        "TerminateJobObject": ([ctypes.c_void_p, ctypes.c_uint32], ctypes.c_int),
        "CloseHandle": ([ctypes.c_void_p], ctypes.c_int),
    }
    for name, (arguments, result) in signatures.items():
        function = getattr(kernel, name)
        function.argtypes, function.restype = arguments, result
    return kernel


class WindowsProcessJob:
    def __init__(self) -> None:
        self._kernel = _kernel()
        self.name = "Local\\redteam-preparation-" + uuid.uuid4().hex
        self._handle = self._kernel.CreateJobObjectW(None, self.name)
        if not self._handle:
            raise OSError("Could not create Windows preparation process job")
        limits = _ExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE (Win32 contract).
        if not self._kernel.SetInformationJobObject(
            self._handle,
            9,
            ctypes.byref(limits),
            ctypes.sizeof(limits),
        ):
            self.close()
            raise OSError("Could not configure Windows preparation process job")

    def command(self, argv: tuple[str, ...]) -> tuple[str, ...]:
        return (sys.executable, "-m", __name__, self.name, *argv)

    def terminate(self) -> None:
        if self._handle:
            self._kernel.TerminateJobObject(self._handle, 1)

    def close(self) -> None:
        if self._handle:
            self._kernel.CloseHandle(self._handle)
            self._handle = None


def _launch(name: str, argv: list[str]) -> int:
    kernel = _kernel()
    handle = kernel.OpenJobObjectW(1, False, name)  # JOB_OBJECT_ASSIGN_PROCESS.
    if not handle:
        return 125
    try:
        if not kernel.AssignProcessToJobObject(handle, kernel.GetCurrentProcess()):
            return 125
    finally:
        kernel.CloseHandle(handle)  # The caller's handle keeps the job alive.
    # Child stdio is inherited; communicate includes descendants that retain it.
    return subprocess.call(argv, stdin=sys.stdin, stdout=sys.stdout, stderr=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(_launch(sys.argv[1], sys.argv[2:]))
