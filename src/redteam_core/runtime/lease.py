from __future__ import annotations

import os
import socket
from pathlib import Path
from typing import BinaryIO

from redteam_core.models.base import canonical_json


class RunLease:
    """Process-scoped advisory single-writer lease for one runtime directory.

    The OS lock, not stale file contents, is authoritative. Locks are released automatically by the
    operating system on process death, so crash recovery does not require deleting a guessed-stale
    PID marker.
    """

    def __init__(self, path: Path, *, run_id: str) -> None:
        self.path = Path(path)
        self.run_id = run_id
        self._stream: BinaryIO | None = None

    @property
    def held(self) -> bool:
        return self._stream is not None

    @staticmethod
    def _lock(stream: BinaryIO) -> None:
        stream.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            return
        import fcntl

        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    @staticmethod
    def _unlock(stream: BinaryIO) -> None:
        stream.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            return
        import fcntl

        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def acquire(self) -> RunLease:
        if self._stream is not None:
            return self
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = self.path.open("a+b")
        try:
            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b"\0")
                stream.flush()
                os.fsync(stream.fileno())
            try:
                self._lock(stream)
            except OSError as exc:
                raise RuntimeError(f"runtime run is already leased: {self.run_id}") from exc
            payload = canonical_json(
                {"run_id": self.run_id, "pid": os.getpid(), "host": socket.gethostname()}
            ).encode("utf-8")
            stream.seek(0)
            stream.truncate()
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        except Exception:
            stream.close()
            raise
        self._stream = stream
        return self

    def release(self) -> None:
        stream = self._stream
        if stream is None:
            return
        try:
            self._unlock(stream)
        finally:
            stream.close()
            self._stream = None

    def __enter__(self) -> RunLease:
        return self.acquire()

    def __exit__(self, *_args: object) -> None:
        self.release()
