"""Filesystem implementation of Track B's internal Artifact backend."""

from __future__ import annotations

import errno
import hashlib
import os
import re
import secrets
import stat
from pathlib import Path

from redteam.evidence.artifacts.base import ArtifactWriteResult
from redteam.evidence.artifacts.errors import (
    ArtifactIntegrityError,
    ArtifactNotFoundError,
    ArtifactReferenceError,
    ArtifactTooLargeError,
    ArtifactWriteError,
)

_REF_PATTERN = re.compile(r"artifact_[0-9a-f]{32}\.bin")
_DEFAULT_MAX_SIZE_BYTES = 50 * 1024 * 1024


class FilesystemArtifactBackend:
    """Store independent immutable blobs below one private directory.

    ``max_size_bytes`` is a temporary local safety default, not an approved
    operating retention or collection policy.
    """

    def __init__(
        self,
        root_dir: str | Path,
        *,
        max_size_bytes: int = _DEFAULT_MAX_SIZE_BYTES,
    ) -> None:
        if max_size_bytes <= 0:
            raise ValueError("max_size_bytes must be positive")
        self._root = Path(root_dir)
        self._root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self._root.is_symlink() or not self._root.is_dir():
            raise ArtifactIntegrityError("artifact root must be a real directory")
        os.chmod(self._root, 0o700)
        self._root_resolved = self._root.resolve(strict=True)
        self._max_size_bytes = max_size_bytes

    def _path_for_ref(self, artifact_ref: str) -> Path:
        if _REF_PATTERN.fullmatch(artifact_ref) is None:
            raise ArtifactReferenceError("invalid artifact reference")
        path = self._root / artifact_ref
        if path.parent.resolve(strict=True) != self._root_resolved:
            raise ArtifactReferenceError("artifact reference escapes storage root")
        return path

    async def put(self, data: bytes, content_type: str) -> ArtifactWriteResult:
        if not isinstance(data, bytes):
            raise TypeError("artifact data must be bytes")
        if not content_type or not content_type.strip():
            raise ValueError("content_type must not be blank")
        if len(data) > self._max_size_bytes:
            raise ArtifactTooLargeError("artifact exceeds configured size limit")

        digest = hashlib.sha256(data).hexdigest()
        artifact_ref = f"artifact_{secrets.token_hex(16)}.bin"
        final_path = self._path_for_ref(artifact_ref)
        temp_path = self._root / f".pending-{secrets.token_hex(16)}"
        fd: int | None = None
        try:
            fd = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as output:
                fd = None
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            # A hard link publishes the complete inode atomically and refuses
            # to replace an existing final name.
            os.link(temp_path, final_path, follow_symlinks=False)
            os.unlink(temp_path)
        except Exception as exc:
            if fd is not None:
                os.close(fd)
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise ArtifactWriteError("artifact write failed before publication") from exc

        return ArtifactWriteResult(
            artifact_ref=artifact_ref,
            digest=digest,
            content_type=content_type,
            size_bytes=len(data),
        )

    async def get(self, artifact_ref: str) -> bytes:
        path = self._path_for_ref(artifact_ref)
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError as exc:
            raise ArtifactNotFoundError("artifact is missing") from exc
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise ArtifactIntegrityError(
                    "artifact path is not a regular stored file"
                ) from exc
            raise ArtifactIntegrityError("artifact could not be safely opened") from exc
        try:
            file_stat = os.fstat(fd)
            if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_nlink != 1:
                raise ArtifactIntegrityError("artifact path is not a regular stored file")
            with os.fdopen(fd, "rb") as source:
                fd = -1
                return source.read()
        finally:
            if fd >= 0:
                os.close(fd)

    async def verify(self, artifact_ref: str, expected_digest: str) -> bool:
        if re.fullmatch(r"[0-9a-f]{64}", expected_digest) is None:
            raise ValueError("expected_digest must be a lowercase SHA-256 hex digest")
        return hashlib.sha256(await self.get(artifact_ref)).hexdigest() == expected_digest
