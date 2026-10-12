"""Runner unsafe-checkpoint reconcile path (no replay, held stays held)."""

from __future__ import annotations


def test_snapshot_pending_work_ids_from_snapshot_fields() -> None:
    from types import SimpleNamespace

    from redteam.orchestration.runner import _snapshot_pending_work_ids

    assert _snapshot_pending_work_ids(None) == set()
    assert _snapshot_pending_work_ids({}) == set()
    values = {
        "current_work_id": "w-current",
        "native_calls": [
            {"completed": False, "dispatch": {"work_id": "w-native"}},
            {"completed": True, "dispatch": {"work_id": "w-done"}},
            {"completed": False, "dispatch": None},
            SimpleNamespace(
                completed=False, dispatch=SimpleNamespace(work_id="w-model")
            ),
        ],
        "dispatch_items": [{"work_id": "w-batch"}, {"other": 1}],
    }
    assert _snapshot_pending_work_ids(values) == {
        "w-current",
        "w-native",
        "w-model",
        "w-batch",
    }
