"""Checkpoint thread identity parsing + enumeration guards."""

from __future__ import annotations

import pytest

from redteam.orchestration.checkpointing import (
    _split_checkpoint_thread,
    list_checkpoint_threads,
)


def test_split_singleton_thread() -> None:
    assert _split_checkpoint_thread("run-1", "run-1") is None


def test_split_instance_thread() -> None:
    assert _split_checkpoint_thread("run-1:instance:a", "run-1") == "a"


def test_split_foreign_threads_rejected() -> None:
    assert _split_checkpoint_thread("other-run", "run-1") is False
    assert _split_checkpoint_thread("run-1:instance:", "run-1") is False
    assert _split_checkpoint_thread("run-1:other:a", "run-1") is False
    assert _split_checkpoint_thread("run-10", "run-1") is False


async def test_enumeration_disabled_returns_empty() -> None:
    assert await list_checkpoint_threads(enabled=False, run_id="run-1") == []


_BLOB_SEQ = [0]


def _checkpoint_blob(runtime_mapping) -> dict:
    # Sequential ids: alist orders by checkpoint_id DESC, so uuid order
    # would make newest/oldest random. Deterministic ids keep latest-first.
    from datetime import datetime, timezone

    _BLOB_SEQ[0] += 1
    return {
        "v": 1,
        "id": f"00000000-0000-0000-0000-{_BLOB_SEQ[0]:012d}",
        "ts": datetime.now(timezone.utc).isoformat(),
        "channel_values": {"runtime": runtime_mapping},
        "channel_versions": {"runtime": 1},
        "versions_seen": {},
    }


async def _write_thread(saver, thread_id: str, runtime_mapping, ns: str = "") -> None:
    await saver.aput(
        {"configurable": {"thread_id": thread_id, "checkpoint_ns": ns}},
        _checkpoint_blob(runtime_mapping),
        {"source": "input", "step": 0},
        {"runtime": 1},
    )


def _runtime_mapping(**overrides) -> dict:
    base = {
        "run_id": "run-1",
        "instance_id": "a",
        "engagement_id": "eng-1",
        "mission_id": "m-1",
        "component_binding": None,
    }
    base.update(overrides)
    return base


def _sqlite_saver(tmp_path):
    from redteam.orchestration.checkpointing import open_checkpointer

    return open_checkpointer(
        registry=None, enabled=True, backend="sqlite", path=str(tmp_path / "t.sqlite3")
    )


async def test_invalid_latest_refuses_without_older_fallback(tmp_path) -> None:
    from redteam.orchestration.checkpointing import CheckpointIdentityError

    async with _sqlite_saver(tmp_path) as saver:
        await _write_thread(saver, "run-1:instance:a", _runtime_mapping())
        await _write_thread(saver, "run-1:instance:a", "not-a-runtime-mapping")
        with pytest.raises(CheckpointIdentityError):
            await list_checkpoint_threads(
                backend="sqlite", path=str(tmp_path / "t.sqlite3"), run_id="run-1"
            )


async def test_wrong_namespace_skipped(tmp_path) -> None:
    async with _sqlite_saver(tmp_path) as saver:
        await _write_thread(saver, "run-1:instance:a", _runtime_mapping(), ns="sub")
        found = await list_checkpoint_threads(
            backend="sqlite", path=str(tmp_path / "t.sqlite3"), run_id="run-1"
        )
    assert found == []


async def test_snapshot_mismatch_refused_not_fallback(tmp_path) -> None:
    from redteam.orchestration.checkpointing import CheckpointIdentityError

    async with _sqlite_saver(tmp_path) as saver:
        await _write_thread(saver, "run-1:instance:a", _runtime_mapping(run_id="run-other"))
        with pytest.raises(CheckpointIdentityError):
            await list_checkpoint_threads(
                backend="sqlite", path=str(tmp_path / "t.sqlite3"), run_id="run-1"
            )


async def test_storage_failure_propagates_bounded(tmp_path) -> None:
    path = tmp_path / "t.sqlite3"
    path.write_bytes(b"not-a-database")
    with pytest.raises(RuntimeError, match="checkpoint enumeration failed"):
        await list_checkpoint_threads(backend="sqlite", path=str(path), run_id="run-1")
