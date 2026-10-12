"""SQLite path revision: snapshots, progress, CAS, restart (new file)."""

from __future__ import annotations

import pytest

from redteam.orchestration.paths import (
    CompletionCriterion,
    PathPlan,
    PathStep,
    PathValidationError,
    PathVersionConflict,
    SQLitePathStore,
)

RUN = "run-path-1"
PATH = "path-1"


def _step(sid, objective="objective-" + "x", **kw):
    base = {
        "step_id": sid,
        "objective": objective if sid == "s-keep" else "objective-" + sid,
        "completion_criteria": [CompletionCriterion(kind="evidence", description="seen-" + sid)],
    }
    base.update(kw)
    return PathStep(**base)


def _chain_step(sid, depends=None, objective=None):
    return PathStep(
        step_id=sid,
        objective=objective or ("objective-" + sid),
        depends_on=list(depends or []),
        allowed_operations=["analysis.command_run"],
        completion_criteria=[CompletionCriterion(kind="evidence", description="seen-" + sid)],
    )


def _plan(version, steps, status="active", parent=None, changed=None, path_id=PATH):
    return PathPlan(
        path_id=path_id,
        run_id=RUN,
        version=version,
        parent_version=parent,
        status=status,
        reason=f"reason-{version}",
        goal="goal",
        steps=steps,
        changed_step_ids=list(changed or []),
    )
















async def test_revision_preserves_identical_completed_step(tmp_path):
    store = SQLitePathStore(tmp_path / "paths.sqlite3")
    v1 = _plan(1, [_step("s-keep"), _step("s-other")])
    await store.register(v1, expected_version=None)
    await store.update_step(
        RUN, PATH, 1, "s-keep", "completed", outcome="done", evidence_ids=["ev-1"]
    )
    v2 = _plan(2, [_step("s-keep"), _step("s-other")], parent=1)
    out = await store.register(v2, expected_version=1)
    kept = {s.step_id: s for s in out.steps}["s-keep"]
    assert kept.status == "completed"
    assert kept.outcome == "done"
    assert kept.evidence_ids == ["ev-1"]
    other = {s.step_id: s for s in out.steps}["s-other"]
    assert other.status == "pending"
    all_versions = await store.list_paths(RUN)
    assert [(p.version, p.status) for p in all_versions] == [(1, "active"), (2, "active")]
    first = [p for p in all_versions if p.version == 1][0]
    assert {s.step_id: s for s in first.steps}["s-keep"].status == "completed"


async def test_changed_definition_without_justification_rejected(tmp_path):
    store = SQLitePathStore(tmp_path / "paths.sqlite3")
    await store.register(_plan(1, [_step("s-keep")]), expected_version=None)
    await store.update_step(RUN, PATH, 1, "s-keep", "completed", evidence_ids=["ev-1"])
    bad = _plan(2, [_step("s-keep", objective="changed-objective")], parent=1)
    with pytest.raises(PathValidationError):
        await store.register(bad, expected_version=1)
    good = _plan(2, [_step("s-keep", objective="changed-objective")], parent=1, changed=["s-keep"])
    out = await store.register(good, expected_version=1)
    reset = {s.step_id: s for s in out.steps}["s-keep"]
    assert reset.status == "pending"
    assert reset.evidence_ids == []
    assert reset.outcome is None
    assert "s-keep" in out.changed_step_ids


async def test_expected_version_cas_mismatch_conflicts(tmp_path):
    store = SQLitePathStore(tmp_path / "paths.sqlite3")
    await store.register(_plan(1, [_step("s-keep")]), expected_version=None)
    with pytest.raises(PathVersionConflict):
        await store.register(_plan(2, [_step("s-keep")], parent=1), expected_version=99)
    with pytest.raises(PathVersionConflict):
        await store.register(_plan(2, [_step("s-keep")], parent=1), expected_version=None)
    with pytest.raises(PathVersionConflict):
        await store.register(_plan(1, [_step("s-keep")]), expected_version=None)
    out = await store.register(_plan(2, [_step("s-keep")], parent=1), expected_version=1)
    assert out.version == 2
    active = await store.get_active(RUN)
    assert active is not None and active.version == 2


async def test_restart_restores_active_progress_and_evidence(tmp_path):
    path = tmp_path / "paths.sqlite3"
    store = SQLitePathStore(path)
    await store.register(_plan(1, [_step("s-keep"), _step("s-other")]), expected_version=None)
    await store.update_step(
        RUN, PATH, 1, "s-keep", "completed", outcome="done", evidence_ids=["ev-1", "ev-2"]
    )
    await store.update_step(RUN, PATH, 1, "s-other", "running")
    reopened = SQLitePathStore(path)
    active = await reopened.get_active(RUN)
    assert active is not None
    assert active.version == 1
    by_id = {s.step_id: s for s in active.steps}
    assert by_id["s-keep"].status == "completed"
    assert by_id["s-keep"].evidence_ids == ["ev-1", "ev-2"]
    assert by_id["s-keep"].outcome == "done"
    assert by_id["s-other"].status == "running"
    versions = await reopened.list_paths(RUN)
    assert len(versions) == 1


async def test_progress_only_on_active_version(tmp_path):
    store = SQLitePathStore(tmp_path / "paths.sqlite3")
    await store.register(_plan(1, [_step("s-keep")]), expected_version=None)
    await store.register(_plan(2, [_step("s-keep")], parent=1), expected_version=1)
    with pytest.raises(PathVersionConflict):
        await store.update_step(RUN, PATH, 1, "s-keep", "completed")
    out = await store.update_step(RUN, PATH, 2, "s-keep", "completed", evidence_ids=["ev-9"])
    assert out is not None
    assert {s.step_id: s for s in out.steps}["s-keep"].evidence_ids == ["ev-9"]


async def test_unaffected_step_carries_over(tmp_path):
    store = SQLitePathStore(tmp_path / "paths.sqlite3")
    v1_steps = [
        _chain_step("s-root"),
        _chain_step("s-child", depends=["s-root"]),
        _chain_step("s-alone"),
    ]
    await store.register(_plan(1, v1_steps), expected_version=None)
    await store.update_step(
        RUN, PATH, 1, "s-root", "completed", outcome="done", evidence_ids=["ev-root"]
    )
    await store.update_step(
        RUN, PATH, 1, "s-alone", "completed", outcome="done", evidence_ids=["ev-alone"]
    )
    v2_steps = [
        _chain_step("s-root", objective="objective-root-changed"),
        _chain_step("s-child", depends=["s-root"]),
        _chain_step("s-alone"),
    ]
    out = await store.register(
        _plan(2, v2_steps, parent=1, changed=["s-root", "s-child"]), expected_version=1
    )
    by_id = {s.step_id: s for s in out.steps}
    assert by_id["s-alone"].status == "completed"
    assert by_id["s-alone"].evidence_ids == ["ev-alone"]
    assert by_id["s-alone"].outcome == "done"


async def test_changed_parent_descendants_reset_even_when_same_shape(tmp_path):
    store = SQLitePathStore(tmp_path / "paths.sqlite3")
    v1_steps = [
        _chain_step("s1"),
        _chain_step("s2", depends=["s1"]),
        _chain_step("s3", depends=["s2"]),
    ]
    await store.register(_plan(1, v1_steps), expected_version=None)
    await store.update_step(RUN, PATH, 1, "s1", "completed", outcome="done", evidence_ids=["ev-1"])
    await store.update_step(RUN, PATH, 1, "s2", "completed", outcome="done", evidence_ids=["ev-2"])
    await store.update_step(RUN, PATH, 1, "s3", "completed", outcome="done", evidence_ids=["ev-3"])
    v2_steps = [
        _chain_step("s1", objective="objective-s1-changed"),
        _chain_step("s2", depends=["s1"]),
        _chain_step("s3", depends=["s2"]),
    ]
    out = await store.register(
        _plan(2, v2_steps, parent=1, changed=["s1", "s2", "s3"]), expected_version=1
    )
    by_id = {s.step_id: s for s in out.steps}
    assert by_id["s1"].status == "pending"
    assert by_id["s1"].evidence_ids == []
    assert by_id["s2"].status == "pending"
    assert by_id["s2"].evidence_ids == []
    assert by_id["s2"].outcome is None
    assert by_id["s3"].status == "pending"
    assert by_id["s3"].evidence_ids == []
    assert by_id["s3"].outcome is None


async def test_history_receipts_kept_after_revision(tmp_path):
    store = SQLitePathStore(tmp_path / "paths.sqlite3")
    v1_steps = [_chain_step("s1"), _chain_step("s2", depends=["s1"])]
    await store.register(_plan(1, v1_steps), expected_version=None)
    await store.update_step(RUN, PATH, 1, "s1", "completed", outcome="done", evidence_ids=["ev-1"])
    v2_steps = [
        _chain_step("s1", objective="objective-s1-changed"),
        _chain_step("s2", depends=["s1"]),
    ]
    await store.register(_plan(2, v2_steps, parent=1, changed=["s1", "s2"]), expected_version=1)
    versions = await store.list_paths(RUN)
    assert [p.version for p in versions] == [1, 2]
    first = [p for p in versions if p.version == 1][0]
    second = [p for p in versions if p.version == 2][0]
    assert {s.step_id: s for s in first.steps}["s1"].status == "completed"
    assert {s.step_id: s for s in first.steps}["s1"].evidence_ids == ["ev-1"]
    assert {s.step_id: s for s in second.steps}["s1"].status == "pending"
