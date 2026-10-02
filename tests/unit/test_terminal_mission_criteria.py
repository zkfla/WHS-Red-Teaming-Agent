"""Natural mission criteria are evaluated against actual task/evidence results."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from redteam.core.contracts.evidence import EvidenceRecord
from redteam.core.contracts.mission import MissionPlan, MissionTask, Provenance
from redteam.core.enums import EvidenceSourceType, MissionStatus, TaskStatus
from redteam.core.ids import EngagementId, EvidenceId, MissionId, RunId, TaskId
from redteam.event_bus.memory import InMemoryEventBus
from redteam.event_bus.publisher import EventPublisher
from redteam.orchestration.coordination import coordinator_for
from redteam.orchestration.paths import InMemoryPathStore, SQLitePathStore
from redteam.orchestration.replanning import finalize_mission
from redteam.orchestration.task_graph.memory import InMemoryTaskGraphStore
from redteam.orchestration.task_graph.sqlite import SQLiteTaskGraphStore
from tests.fakes import FakeEvidenceStore

RUN = RunId("run-terminal")
MISSION = MissionId("mission-terminal")
ENGAGEMENT = EngagementId("eng-terminal")
TASK = TaskId("task-terminal")
CRITERIA = ["허용된 호스트의 서비스 목록을 관측한다", "관측한 서비스의 응답을 확인한다"]


class TerminalLLM:
    def __init__(self, *, unmet=False, evidence_id="actual-proof", task_id=str(TASK), hook=None):
        self.calls = []
        self.unmet = unmet
        self.evidence_id = evidence_id
        self.task_id = task_id
        self.hook = hook

    async def complete_structured(self, **kwargs):
        self.calls.append(kwargs)
        if self.hook is not None and len(self.calls) == 1:
            await self.hook()
        return {
            "accepted": True,
            "reason": "The actual observation supports this assessment.",
            "criteria": [
                {
                    "criterion": criterion,
                    "met": not (self.unmet and index == 1),
                    "task_ids": [self.task_id],
                    "evidence_ids": [self.evidence_id],
                    "reason": "Actual measured service response.",
                }
                for index, criterion in enumerate(CRITERIA)
            ],
        }


@pytest.fixture(params=["memory", "sqlite"])
async def world(request, tmp_path):
    now = datetime.now(timezone.utc)
    if request.param == "memory":
        store, paths = InMemoryTaskGraphStore(), InMemoryPathStore()
    else:
        db = tmp_path / "orchestration.sqlite3"
        store, paths = SQLiteTaskGraphStore(db), SQLitePathStore(db)
    await store.create_mission(
        MissionPlan(
            mission_id=MISSION,
            engagement_id=ENGAGEMENT,
            objective="서비스 응답 관측",
            success_criteria=CRITERIA,
            status=MissionStatus.ACTIVE,
            provenance=Provenance(source="test:terminal"),
            created_at=now,
            updated_at=now,
        )
    )
    await store.create_task(
        MissionTask(
            task_id=TASK,
            mission_id=MISSION,
            title="Observe service",
            objective="Observe service",
            status=TaskStatus.COMPLETED,
            provenance=Provenance(source="test:terminal"),
            created_at=now,
            updated_at=now,
        )
    )
    evidence = FakeEvidenceStore()
    await evidence.append(
        EvidenceRecord(
            evidence_id=EvidenceId("actual-proof"),
            run_id=RUN,
            engagement_id=ENGAGEMENT,
            source_type=EvidenceSourceType.TOOL,
            source_id="recon.service_discovery",
            content_type="application/json",
            digest="actual-observation",
            created_at=now,
            summary="The authorized host returned its measured service inventory.",
            metadata={"success": True, "mission_task_id": str(TASK)},
        )
    )
    coordinator = coordinator_for(store, paths)
    await coordinator.bind(str(RUN), str(MISSION), str(ENGAGEMENT))
    publisher = EventPublisher(
        InMemoryEventBus(), run_id=RUN, engagement_id=ENGAGEMENT, source="test:terminal"
    )
    return store, paths, evidence, publisher, coordinator


async def settle(world, llm, **kwargs):
    store, paths, evidence, publisher, _ = world
    return await finalize_mission(
        store,
        MISSION,
        publisher=publisher,
        last_event_id=None,
        evidence_store=evidence,
        run_id=RUN,
        path_store=paths,
        llm_client=llm,
        **kwargs,
    )


async def test_natural_criteria_store_actual_task_and_evidence_links(world):
    llm = TerminalLLM()
    event_id = await settle(world, llm)
    store, _, _, _, coordinator = world
    mission = await store.get_mission(MISSION)
    assert mission.status is MissionStatus.COMPLETED
    assert [result.criterion for result in mission.criterion_results] == CRITERIA
    assert all(
        result.met and result.task_ids == [TASK] and result.evidence_ids == ["actual-proof"]
        for result in mission.criterion_results
    )
    assert len(llm.calls) == 1

    async def inspect(change):
        assert change.binding(str(RUN), str(MISSION))["closed"]
        assert ("outbox", str(event_id)) in change.records

    await coordinator.change(inspect)
    # Stored engine/terminal validated completion does not need another LLM call.
    await settle(world, llm)
    assert len(llm.calls) == 1


async def test_one_unmet_criterion_blocks_mission_even_after_tasks_complete(world):
    await settle(world, TerminalLLM(unmet=True))
    mission = await world[0].get_mission(MISSION)
    assert mission.status is MissionStatus.BLOCKED
    assert [item.met for item in mission.criterion_results] == [True, False]


@pytest.mark.parametrize("mismatch", ["evidence", "task"])
async def test_invented_or_wrong_task_reference_cannot_satisfy_criteria(world, mismatch):
    llm = TerminalLLM(
        evidence_id="foreign-evidence" if mismatch == "evidence" else "actual-proof",
        task_id="another-task" if mismatch == "task" else str(TASK),
    )
    await settle(world, llm, decision_max_attempts=2)
    mission = await world[0].get_mission(MISSION)
    assert mission.status is MissionStatus.BLOCKED
    assert not any(item.met for item in mission.criterion_results)
    assert len(llm.calls) == 2
    assert all(
        "terminal_criterion_evidence_binding" in item.reason for item in mission.criterion_results
    )


async def test_no_qualifying_evidence_does_not_call_model_or_claim_completion(world):
    store, paths, _, publisher, _ = world
    llm = TerminalLLM()
    await finalize_mission(
        store,
        MISSION,
        publisher=publisher,
        last_event_id=None,
        run_id=RUN,
        path_store=paths,
        llm_client=llm,
        evidence_store=FakeEvidenceStore(),
    )
    mission = await store.get_mission(MISSION)
    assert mission.status is MissionStatus.BLOCKED
    assert all(
        not item.met and item.reason == "completed_task_evidence_missing"
        for item in mission.criterion_results
    )
    assert not llm.calls


@pytest.mark.parametrize("status", [MissionStatus.CANCELLED, MissionStatus.FAILED])
async def test_closed_outcome_preserved_with_per_criterion_evaluation(world, status):
    store = world[0]
    mission = await store.get_mission(MISSION)
    await store.update_mission(
        mission.model_copy(
            update={
                "status": status,
                "revision": mission.revision + 1,
            }
        )
    )
    await settle(world, TerminalLLM(unmet=True))
    latest = await store.get_mission(MISSION)
    assert latest.status is status
    assert len(latest.criterion_results) == len(CRITERIA)
    assert not latest.criterion_results[1].met


async def test_new_required_task_during_review_invalidates_completion_without_extra_budget(world):
    store = world[0]

    async def add_work():
        task = await store.get_task(TASK)
        await store.create_task(
            task.model_copy(
                update={
                    "task_id": TaskId("late-required"),
                    "status": TaskStatus.READY,
                }
            )
        )

    llm = TerminalLLM(hook=add_work)
    await settle(world, llm, decision_max_attempts=1)
    latest = await store.get_mission(MISSION)
    assert latest.status is MissionStatus.BLOCKED
    assert not any(result.met for result in latest.criterion_results)
    assert len(llm.calls) == 1
    assert "terminal_reviewed_state_changed" in latest.criterion_results[0].reason


async def test_missing_model_never_passes_natural_criteria(world):
    await settle(world, None)
    latest = await world[0].get_mission(MISSION)
    assert latest.status is MissionStatus.BLOCKED
    assert all(item.reason == "terminal_review_unavailable" for item in latest.criterion_results)


async def test_refused_semantic_review_records_safe_failure_cause(world):
    class RefusingLLM(TerminalLLM):
        async def complete_structured(self, **kwargs):
            response = await super().complete_structured(**kwargs)
            response["accepted"] = False
            return response

    await settle(world, RefusingLLM())
    latest = await world[0].get_mission(MISSION)
    assert latest.status is MissionStatus.BLOCKED
    assert not any(item.met for item in latest.criterion_results)

    async def inspect(change):
        diagnostic = change.records[("terminal_review", str(RUN))]
        assert diagnostic["code"] == "terminal_review_rejected"
        assert diagnostic["retryable"] is False

    await world[4].change(inspect)


@pytest.mark.parametrize("field", ["run_id", "engagement_id", "mission_task_id", "success"])
async def test_wrong_run_engagement_task_or_failed_tool_evidence_is_ineligible(world, field):
    record = world[2].records[0]
    updates = {
        "run_id": RunId("foreign-run"),
        "engagement_id": EngagementId("foreign-engagement"),
        "mission_task_id": "foreign-task",
        "success": False,
    }
    if field in {"run_id", "engagement_id"}:
        world[2].records[0] = record.model_copy(update={field: updates[field]})
    else:
        world[2].records[0] = record.model_copy(
            update={"metadata": {**record.metadata, field: updates[field]}}
        )
    llm = TerminalLLM()
    await settle(world, llm)
    latest = await world[0].get_mission(MISSION)
    assert latest.status is MissionStatus.BLOCKED
    assert not any(item.met for item in latest.criterion_results)
    assert not llm.calls
