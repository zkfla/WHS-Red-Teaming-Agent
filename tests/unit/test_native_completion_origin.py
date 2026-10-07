from __future__ import annotations

from types import SimpleNamespace

import pytest

from redteam.orchestration.completion import verify_origin_step, verify_stored_origin
from redteam.orchestration.completion_proposal import CompletionProposal, record_unresolved
from redteam.orchestration.coordination import content_hash
from redteam.orchestration.semantic_completion import select_task_evidence_ids

RUN = "run-native-1"
MISSION = "mission-native-1"
ENGAGEMENT = "eng-native-1"
TASK_ID = "task-native-1"
OBJECTIVE = "confirm the login session behavior on node web-1"


def _purpose():
    return content_hash({"objective": OBJECTIVE})


def _native_meta(**overrides):
    base = {
        "execution_origin_version": 1,
        "model_tool_call_id": "call-abc",
        "mission_task_id": TASK_ID,
        "task_purpose_hash": _purpose(),
        "mission_id": MISSION,
        "work_id": "work-1",
        "candidate_key": "cand-1",
    }
    base.update(overrides)
    return base


def _work(**overrides):
    base = {
        "run_id": RUN,
        "mission_task_id": TASK_ID,
        "candidate_key": "cand-1",
        "model_tool_call_id": "call-abc",
        "task_purpose_hash": _purpose(),
        "execution_origin_version": 1,
        "target_node_ids": ["node-web-1"],
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _task(**overrides):
    base = {"task_id": TASK_ID, "mission_id": MISSION, "objective": OBJECTIVE}
    base.update(overrides)
    return SimpleNamespace(**base)


class _WorkStore:
    def __init__(self, work):
        self._work = work

    async def get(self, work_id):
        if self._work is None:
            return None
        return self._work if work_id == "work-1" else None


class _TaskStore:
    def __init__(self, task):
        self._task = task

    async def get_task(self, task_id):
        return self._task if task_id == TASK_ID else None


async def _check(meta, task, work):
    return await verify_stored_origin(
        path_store=None,
        run_id=RUN,
        m_path_id="",
        m_version=None,
        m_step_id="",
        mission_id=MISSION,
        metadata=meta,
        task_graph_store=_TaskStore(task),
        work_store=_WorkStore(work),
    )


@pytest.mark.asyncio
async def test_native_pathless_admits_with_matching_work_and_task():
    assert await _check(_native_meta(), _task(), _work()) == []


@pytest.mark.asyncio
async def test_native_rejects_task_purpose_drift():
    errors = await _check(_native_meta(), _task(objective="a different objective"), _work())
    assert errors and "purpose changed" in errors[0]


@pytest.mark.asyncio
async def test_native_rejects_work_call_mismatch():
    meta = _native_meta(model_tool_call_id="call-other")
    errors = await _check(meta, _task(), _work())
    assert errors and "disagrees" in errors[0]


@pytest.mark.asyncio
async def test_native_missing_work_record_is_unproven():
    errors = await _check(_native_meta(), _task(), None)
    assert errors and "work record is missing" in errors[0]


@pytest.mark.asyncio
async def test_native_rejects_task_only_without_work_binding():
    # A live task alone proves delegation, not execution: no work record
    # may ever admit a native origin.
    meta = _native_meta()
    del meta["work_id"]
    errors = await _check(meta, _task(), _work())
    assert errors and "no work binding" in errors[0]


@pytest.mark.asyncio
async def test_native_rejects_marker_only_without_stores():
    # Bare metadata markers admit nothing when no store can prove them.
    errors = await verify_stored_origin(
        path_store=None,
        run_id=RUN,
        m_path_id="",
        m_version=None,
        m_step_id="",
        mission_id=MISSION,
        metadata=_native_meta(),
        task_graph_store=None,
        work_store=None,
    )
    assert errors and "unavailable" in errors[0]


@pytest.mark.asyncio
async def test_native_rejects_missing_call_binding():
    # Fail closed: a missing call id on either side skips no comparison.
    meta = _native_meta()
    del meta["model_tool_call_id"]
    errors = await _check(meta, _task(), _work())
    assert errors and "has no binding" in errors[0]


@pytest.mark.asyncio
async def test_native_rejects_missing_purpose_binding():
    meta = _native_meta()
    del meta["task_purpose_hash"]
    errors = await _check(meta, _task(), _work())
    assert errors and "no binding" in errors[0]


@pytest.mark.asyncio
async def test_native_rejects_cross_run_work():
    errors = await _check(_native_meta(), _task(), _work(run_id="run-other"))
    assert errors and "another run" in errors[0]


@pytest.mark.asyncio
async def test_native_rejects_work_without_target_binding():
    errors = await _check(_native_meta(), _task(), _work(target_node_ids=[]))
    assert errors and "no target binding" in errors[0]


def _target_free_meta():
    return _native_meta(
        action_id="action-search", invocation_id="inv-search",
        proposal={
            "action_id": "action-search", "run_id": RUN, "engagement_id": ENGAGEMENT,
            "agent_id": "research", "binding_mode": "selected_tool", "tool_id": "search",
            "target": None, "arguments": {"query": "native tools"}, "reason": OBJECTIVE,
            "created_at": "2026-10-07T00:00:00Z",
        },
        tool_result={
            "action_id": "action-search", "invocation_id": "inv-search", "tool_id": "search",
        },
    )


@pytest.mark.asyncio
async def test_native_target_free_origin_keeps_work_and_raw_binding():
    assert await _check(_target_free_meta(), _task(), _work(target_node_ids=[])) == []


@pytest.mark.asyncio
async def test_native_origin_can_be_cited_by_a_legacy_path_step():
    assert await verify_origin_step(
        path_store=None, run_id=RUN,
        path=SimpleNamespace(path_id="legacy", version=1), step=SimpleNamespace(step_id="step"),
        m_path_id="", m_version=None, m_step_id="", evidence_id="evidence-search",
        metadata=_target_free_meta(), task_graph_store=_TaskStore(_task()),
        work_store=_WorkStore(_work(target_node_ids=[])), mission_id=MISSION,
    ) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["missing_target", "different_invocation", "invented_target"])
async def test_native_target_free_origin_rejects_incomplete_or_inconsistent_input(damage):
    meta = _target_free_meta()
    if damage == "missing_target":
        del meta["proposal"]["target"]
    elif damage == "different_invocation":
        meta["tool_result"]["invocation_id"] = "another-invocation"
    else:
        meta["target_node_id"] = "invented-node"
    assert await _check(meta, _task(), _work(target_node_ids=[]))


@pytest.mark.asyncio
async def test_native_rejects_cross_run_task():
    errors = await _check(_native_meta(), _task(mission_id="mission-other"), _work())
    assert errors and "another mission" in errors[0]


@pytest.mark.asyncio
async def test_native_missionless_work_admits_basic_origin():
    # Goal-only observation: proven work plus a matching goal purpose hash,
    # with no mission and no fabricated task. This admits the basic origin
    # only; successful mission completion still needs its own task receipt
    # in the settlement layers above.
    meta = _native_meta()
    del meta["mission_task_id"]
    del meta["mission_id"]
    work = _work(mission_task_id="")
    errors = await verify_stored_origin(
        path_store=None,
        run_id=RUN,
        m_path_id="",
        m_version=None,
        m_step_id="",
        mission_id=None,
        metadata=meta,
        task_graph_store=_TaskStore(None),
        work_store=_WorkStore(work),
    )
    assert errors == []


@pytest.mark.asyncio
async def test_native_missionless_shape_rejected_once_mission_set():
    # The same task-free shape must not launder into a mission context.
    meta = _native_meta()
    del meta["mission_task_id"]
    del meta["mission_id"]
    work = _work(mission_task_id="")
    errors = await verify_stored_origin(
        path_store=None,
        run_id=RUN,
        m_path_id="",
        m_version=None,
        m_step_id="",
        mission_id=MISSION,
        metadata=meta,
        task_graph_store=_TaskStore(None),
        work_store=_WorkStore(work),
    )
    assert errors and "no task binding" in errors[0]


@pytest.mark.asyncio
async def test_legacy_evidence_still_requires_path_linkage():
    errors = await verify_stored_origin(
        path_store=None,
        run_id=RUN,
        m_path_id="",
        m_version=None,
        m_step_id="",
        mission_id=MISSION,
        metadata={"execution_origin_version": 0},
    )
    assert errors == ["has no path linkage (path_id/path_version/path_step_id required)"]


def test_select_task_evidence_ids_accepts_path_none():
    decision = SimpleNamespace(completion={"evidence_ids": ["e1", "e2"]}, reason="done")
    assert select_task_evidence_ids(decision, None) == ["e1", "e2"]
    empty = SimpleNamespace(completion={}, reason="done")
    assert select_task_evidence_ids(empty, None) == []


class _EvidenceStore:
    def __init__(self):
        self.saved = []

    async def append(self, record):
        self.saved.append(record)
        return record


@pytest.mark.asyncio
async def test_record_unresolved_accepts_path_none():
    store = _EvidenceStore()
    runtime = SimpleNamespace(mission_id=MISSION, run_id=RUN, engagement_id=ENGAGEMENT)
    proposal = CompletionProposal(outcome="inconclusive", reason="need more signal")
    identity = await record_unresolved(
        evidence_store=store,
        runtime=runtime,
        proposal=proposal,
        path=None,
        task_id=TASK_ID,
    )
    assert identity
    payload = store.saved[0].metadata
    assert payload["path_id"] is None
    assert payload["path_version"] is None
    assert payload["task_id"] == TASK_ID
