"""Windows file-handle ownership for the SQLite backend's Run companion.

The handle denies sharing and deletion until close/process exit. It is not a
Task claim, a database transaction, or authority to repeat external effects.
"""

from __future__ import annotations

import importlib
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from redteam.orchestration.coordination import CoordinationError


@contextmanager
def exclusive_companion(path: Path, *, create: bool = True) -> Iterator[None]:
    # Resolve Windows-only APIs lazily; importing the backend on POSIX must
    # not require WinDLL. Explicit ABI widths also support 64-bit handles.
    ctypes: Any = importlib.import_module("ctypes")
    wintypes: Any = importlib.import_module("ctypes.wintypes")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)

    class FileInformation(ctypes.Structure):
        _fields_ = [
            ("attributes", wintypes.DWORD),
            ("creation", wintypes.FILETIME),
            ("access", wintypes.FILETIME),
            ("write", wintypes.FILETIME),
            ("volume", wintypes.DWORD),
            ("size_high", wintypes.DWORD),
            ("size_low", wintypes.DWORD),
            ("links", wintypes.DWORD),
            ("index_high", wintypes.DWORD),
            ("index_low", wintypes.DWORD),
        ]

    create_file = kernel.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    inspect = kernel.GetFileInformationByHandle
    inspect.argtypes = [wintypes.HANDLE, ctypes.POINTER(FileInformation)]
    inspect.restype = wintypes.BOOL
    file_type = kernel.GetFileType
    file_type.argtypes = [wintypes.HANDLE]
    file_type.restype = wintypes.DWORD
    close = kernel.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL

    # OPEN_ALWAYS never truncates an existing companion. Zero sharing denies
    # contenders and live replacement; NULL security attributes do not inherit
    # the handle into child processes. Open the reparse point itself for checking.
    generic_read_write = 0x80000000 | 0x40000000
    disposition = 4 if create else 3  # OPEN_ALWAYS or OPEN_EXISTING
    open_reparse_point = 0x00200000
    handle = create_file(str(path), generic_read_write, 0, None, disposition,
                    open_reparse_point, None)
    if handle == ctypes.c_void_p(-1).value:
        error = ctypes.get_last_error()
        if not create and error in {2, 3}:  # absent read-only companion, never create it
            yield
            return
        if error == 32:  # ERROR_SHARING_VIOLATION
            raise CoordinationError("run_execution_already_owned")
        raise CoordinationError("run_execution_storage_unavailable")
    try:
        information = FileInformation()
        if not inspect(handle, ctypes.byref(information)):
            raise CoordinationError("run_execution_storage_unavailable")
        directory_or_reparse = 0x10 | 0x400
        if (file_type(handle) != 1  # FILE_TYPE_DISK
                or information.attributes & directory_or_reparse
                or information.links != 1):
            raise CoordinationError("run_execution_storage_unsafe")
        yield
    finally:
        if not close(handle):
            raise CoordinationError("run_execution_storage_unavailable")
