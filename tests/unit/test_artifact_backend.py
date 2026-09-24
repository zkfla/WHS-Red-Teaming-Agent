from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path

import pytest

from redteam.evidence.artifacts import (
    ArtifactIntegrityError,
    ArtifactNotFoundError,
    ArtifactReferenceError,
    ArtifactWriteError,
    FilesystemArtifactBackend,
)


async def test_put_get_and_verify_exact_bytes(tmp_path: Path) -> None:
    backend = FilesystemArtifactBackend(tmp_path)
    raw = b"HTTP/1.1 200 OK\r\nSet-Cookie: raw-secret\r\n\r\n\x00body"

    stored = await backend.put(raw, "application/http")

    assert await backend.get(stored.artifact_ref) == raw
    assert stored.digest == hashlib.sha256(raw).hexdigest()
    assert stored.content_type == "application/http"
    assert stored.size_bytes == len(raw)
    assert await backend.verify(stored.artifact_ref, stored.digest)


async def test_same_bytes_receive_independent_refs(tmp_path: Path) -> None:
    backend = FilesystemArtifactBackend(tmp_path)
    first, second = await asyncio.gather(
        backend.put(b"same", "text/plain"), backend.put(b"same", "text/plain")
    )

    assert first.artifact_ref != second.artifact_ref
    assert first.digest == second.digest
    assert len(list(tmp_path.glob("artifact_*.bin"))) == 2


@pytest.mark.parametrize(
    "reference",
    ["../outside", "/tmp/outside", "https://example.test/raw", "artifact_bad.bin"],
)
async def test_invalid_or_escaping_reference_is_rejected(
    tmp_path: Path, reference: str
) -> None:
    backend = FilesystemArtifactBackend(tmp_path)
    with pytest.raises(ArtifactReferenceError):
        await backend.get(reference)


async def test_missing_and_tampered_artifact_are_distinguished(tmp_path: Path) -> None:
    backend = FilesystemArtifactBackend(tmp_path)
    with pytest.raises(ArtifactNotFoundError):
        await backend.get("artifact_00000000000000000000000000000000.bin")

    stored = await backend.put(b"original", "application/octet-stream")
    (tmp_path / stored.artifact_ref).write_bytes(b"changed")
    assert not await backend.verify(stored.artifact_ref, stored.digest)


async def test_symlink_to_file_outside_root_is_rejected(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-artifact"
    outside.write_bytes(b"outside secret")
    reference = "artifact_11111111111111111111111111111111.bin"
    (tmp_path / reference).symlink_to(outside)
    backend = FilesystemArtifactBackend(tmp_path)

    with pytest.raises(ArtifactIntegrityError):
        await backend.get(reference)


async def test_write_failure_exposes_no_final_or_pending_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = FilesystemArtifactBackend(tmp_path)
    secret = b"must-not-appear-in-error"

    def fail_link(*_args: object, **_kwargs: object) -> None:
        raise OSError("simulated link failure")

    monkeypatch.setattr(os, "link", fail_link)
    with pytest.raises(ArtifactWriteError) as exc_info:
        await backend.put(secret, "text/plain")

    assert secret.decode() not in str(exc_info.value)
    assert list(tmp_path.iterdir()) == []


async def test_artifact_permissions_are_private(tmp_path: Path) -> None:
    backend = FilesystemArtifactBackend(tmp_path)
    stored = await backend.put(b"private", "text/plain")

    assert (tmp_path.stat().st_mode & 0o777) == 0o700
    assert ((tmp_path / stored.artifact_ref).stat().st_mode & 0o777) == 0o600
