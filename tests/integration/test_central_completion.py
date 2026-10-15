"""2d: temporary originals and explicit protocol doubles, never live calls."""

import asyncio
import json
import sqlite3
from contextlib import asynccontextmanager

import pytest

from redteam.app.central_run import open_central_run
from redteam.app.central_views import inspect_central_run
from redteam.attack_graph.service import AttackGraphService
from redteam.capability.development import DevelopmentResponse
from redteam.core.config import AppConfig, EngagementConfig
from redteam.core.contracts.events import EventType
from redteam.core.contracts.graph import Hypothesis
from redteam.core.enums import AgentPhase, MissionStatus, TaskStatus
from redteam.llm.fake import FakeLLMClient
from redteam.orchestration.central_completion import complete_goal, goal_inputs
from redteam.orchestration.central_results import settle_central
from redteam.orchestration.completion import origin_row_hash
from redteam.orchestration.completion_proposal import CompletionProposal
from redteam.orchestration.coordination import StateConflict, content_hash
from redteam.orchestration.local_tools import LocalToolContext
from redteam.orchestration.state import TerminationReason
from tests.integration.test_central_development import engagement, gateway
from tests.integration.test_central_parallel import limits
from tests.integration.test_central_recovery import start


class OriginalProtocolDouble:
    """Explicit synthetic provider implementing the normal ToolGateway contract.

    Unlike the development lane, these tests exercise production-shaped
    originals and review verification. Nothing here contacts a real target.
    """

    def __init__(self, inner):
        self.inner, self.catalog = inner, inner.catalog

    async def invoke(self, invocation):
        result = await self.inner.invoke(invocation)
        return result.model_copy(update={"metadata": {
            key: value for key, value in result.metadata.items()
            if key not in {"development_double", "mock"}
        }})


@asynccontextmanager
async def selection(tmp_path, *, tools=None, llm=None):
    original = engagement()
    original.metadata["success_criteria"] = ["Assess the original response"]
    config = AppConfig(engagement=EngagementConfig.model_validate(
        original.model_dump(exclude={"engagement_id", "created_at"}),
    ))
    llm = llm or FakeLLMClient()
    tools = tools or OriginalProtocolDouble(gateway())
    async with open_central_run(
        config, directory=tmp_path / "run", settings=limits()[1],
        llm=llm, tool_gateway=tools, tool_catalog=tools.catalog,
    ) as session:
        yield session, llm


async def finish_inconclusive(session, llm, *, questions=()):
    count = len(await session.deps.central_state.list_tasks())
    original, = await start(session, llm, registration_prefix=f"registration-{count}")
    records = await session.deps.evidence_store.list_for_run(original.run_id)
    originals = [str(record.evidence_id) for record in records
                 if isinstance(record.metadata.get("tool_result"), dict)
                 and record.metadata.get("mission_task_id") == str(original.active_task_id)]
    assert originals
    if questions:
        from redteam.orchestration.central_results import publish_result_handler
        published = await publish_result_handler({"result": {
            "result_id": "result-" + str(original.active_task_id),
            "outcome": "inconclusive", "summary": "Original response inspected",
            "evidence_ids": originals, "questions": list(questions),
        }}, LocalToolContext(
            session.deps, original.model_copy(update={"phase": AgentPhase.OBSERVE}),
        ))
        assert published.ok, published.error
    outcome = await settle_central(
        "task", CompletionProposal(outcome="inconclusive", reason="Observed response recorded",
                                   evidence_ids=originals),
        LocalToolContext(session.deps, original.model_copy(update={"phase": AgentPhase.OBSERVE})),
    )
    assert outcome.ok, outcome.error
    return originals


async def review(session, llm, originals, *, met=True):
    mission = await session.deps.central_state.get_mission(session.runtime.mission_id)
    llm._canned_structured = {
        "accepted": met, "reason": "Criteria judged from preserved originals",
        "criteria": [{"criterion": criterion, "met": met, "evidence_ids": originals,
                      "claims": [{"kind": "semantic", "description": "Response assessed",
                                  "evidence_ids": originals}] if met else []}
                     for criterion in mission.success_criteria],
    }
    await session.deps.central_state.transaction(lambda tx: tx.set_settlement_gate(True))
    return await complete_goal(session.deps, session.runtime)


async def test_goal_uses_original_criteria_without_requiring_finding(tmp_path):
    async with selection(tmp_path) as (session, llm):
        originals = await finish_inconclusive(session, llm, questions=[{
            "question_id": "optional", "question": "Explore another response later",
        }])
        graph = AttackGraphService(session.deps.graph_store)
        await graph.record_hypothesis(
            Hypothesis(hypothesis_id="optional", description="Later idea"),
        )
        assert await review(session, llm, originals) == []
        mission = await session.deps.central_state.get_mission(session.runtime.mission_id)
        assert mission.status is MissionStatus.COMPLETED
        assert mission.termination_reason == TerminationReason.GOAL_ACHIEVED
        assert mission.criterion_results[0].goal_revision == mission.revision - 1
        _, _, report = await inspect_central_run(session.deps.central_state.path,
                                                str(session.runtime.run_id))
        assert report.ready_for_release and report.findings == []
        assert report.deferred_hypotheses == ["optional"]
        assert report.effective_limits["workers"] == 2
        assert report.criterion_evidence[0]["evidence_ids"] == originals
        assert not report.unresolved_questions[0]["resolved"]
        assert not report.unresolved_questions[0]["required_for_goal"]


async def test_goal_rechecks_used_optional_origin_task_row_during_review(tmp_path):
    """A used optional Task's exact source row stays fenced through goal review."""
    async with selection(tmp_path) as (session, llm):
        originals = await finish_inconclusive(session, llm)
        state = session.deps.central_state
        source, = await state.list_tasks()
        # Make the completed producer optional; its original is still selected by
        # the goal review below and must therefore retain a source-row read.
        await state.transaction(lambda tx: tx._write_task(
            tx.get_task(str(source.task_id)).model_copy(update={"required": False}),
        ))
        source_before_review = await state.get_task(str(source.task_id))
        source_hash_before_review = origin_row_hash(source_before_review)
        mission = await state.get_mission(session.runtime.mission_id)
        llm._canned_structured = {
            "accepted": True,
            "reason": "The optional original supports the goal criterion",
            "criteria": [{
                "criterion": criterion,
                "met": True,
                "evidence_ids": originals,
                "claims": [{
                    "kind": "semantic",
                    "description": "The preserved response supports the criterion",
                    "evidence_ids": originals,
                }],
            } for criterion in mission.success_criteria],
        }
        await state.transaction(lambda tx: tx.set_settlement_gate(True))

        async def terminal_event_count():
            return await state.transaction(lambda tx: sum(
                (envelope := json.loads(row[0])).get("event_type") == EventType.TASK_COMPLETED
                or (
                    envelope.get("event_type") == "MissionUpdated"
                    and envelope.get("payload", {}).get("status") == "completed"
                )
                for row in tx.db.execute(
                    "SELECT payload FROM state_outbox WHERE run_id=?", (tx.run_id,),
                )
            ), write=False)

        before_count = await terminal_event_count()
        before_mission = await state.get_mission(session.runtime.mission_id)
        before = len(llm.requests)
        llm.request_release.clear()
        pending = asyncio.create_task(complete_goal(session.deps, session.runtime))
        try:
            await asyncio.wait_for(llm.wait_for_requests(before + 1), 5)

            def change_source(tx):
                current = tx.get_task(str(source.task_id))
                # Title changes keep the source purpose and receipt valid, so the
                # specific row hash—not a purpose check—must reject admission.
                tx._write_task(current.model_copy(update={"title": "Changed during review"}))

            await state.transaction(change_source)
        finally:
            llm.request_release.set()

        with pytest.raises(StateConflict, match="goal_origin_task_changed_reassessment_required"):
            await pending
        after_mission = await state.get_mission(session.runtime.mission_id)
        assert after_mission == before_mission
        assert content_hash(after_mission) == content_hash(before_mission)
        actual_source_hash = origin_row_hash(await state.get_task(str(source.task_id)))
        assert actual_source_hash != source_hash_before_review
        assert await terminal_event_count() == before_count


@pytest.mark.parametrize(
    "case", ["criteria", "evidence", "required_question", "unknown", "blocked"],
)
async def test_missing_essential_inputs_never_commit_success(tmp_path, case):
    tools = OriginalProtocolDouble(gateway(unknown=case == "unknown"))
    async with selection(tmp_path, tools=tools) as (session, llm):
        if case == "unknown":
            await start(session, llm)
        else:
            await finish_inconclusive(session, llm)
        state = session.deps.central_state
        if case == "criteria":
            mission = await state.get_mission(session.runtime.mission_id)
            await state.update_mission(mission.model_copy(update={
                "success_criteria": [], "revision": mission.revision + 1,
            }))
        elif case in {"evidence", "required_question", "blocked"}:
            def alter(tx):
                task, = tx.list_tasks()
                result = task.result.model_dump(mode="json")
                if case == "evidence":
                    result["evidence_ids"] = []
                elif case == "blocked":
                    result["outcome"] = "blocked"
                else:
                    result["questions"] = [{"question_id": "needed", "question": "Verify access",
                                            "required_for_goal": True, "reason": "Goal needs it"}]
                payload = task.model_dump(mode="json")
                payload["result"] = result
                tx.db.execute("UPDATE state_tasks SET payload=? WHERE task_id=?",
                              (json.dumps(payload), str(task.task_id)))
            await state.transaction(alter)
        _, _, _, errors, *_ = await goal_inputs(session.deps, session.runtime)
        assert errors
        assert (await state.get_mission(session.runtime.mission_id)).status is MissionStatus.ACTIVE


@pytest.mark.parametrize("related", [True, False])
async def test_review_rechecks_related_inputs_but_allows_unrelated_graph(tmp_path, related):
    async with selection(tmp_path) as (session, llm):
        originals = await finish_inconclusive(session, llm)
        before = len(llm.requests)
        llm.request_release.clear()
        pending = asyncio.create_task(review(session, llm, originals))
        await asyncio.wait_for(llm.wait_for_requests(before + 1), 3)
        if related:
            mission = await session.deps.central_state.get_mission(session.runtime.mission_id)
            await session.deps.central_state.update_mission(mission.model_copy(update={
                "revision": mission.revision + 1, "objective": "Changed goal",
            }))
        else:
            await AttackGraphService(session.deps.graph_store).record_hypothesis(
                Hypothesis(hypothesis_id="unrelated", description="Optional later question"),
            )
        llm.request_release.set()
        if related:
            with pytest.raises(StateConflict, match="reassessment_required"):
                await pending
        else:
            assert await pending == []


async def test_actual_creation_registers_cleanup_and_blocks_success(tmp_path):
    inner = gateway()
    inner.responses[0] = DevelopmentResponse(
        output={"resource_id": "synthetic-object", "created": True},
        metadata={"resource_effects": [{"version": 2, "identity_domain": "synthetic-backend",
                                       "effect": "created", "resource_type": "object",
                                       "identity_path": ["resource_id"],
                                       "confirmation_path": ["created"]}]},
    )
    async with selection(tmp_path, tools=OriginalProtocolDouble(inner)) as (session, llm):
        await finish_inconclusive(session, llm)
        report = await AttackGraphService(session.deps.graph_store).cleanup_report()
        assert len(report.outstanding) == 1
        item, = report.items
        assert item.created_evidence_id and item.created_by_action_id
        _, _, _, errors, *_ = await goal_inputs(session.deps, session.runtime)
        assert "required_cleanup_outstanding" in errors
        # A graph write alone is not terminal cleanup proof.
        await AttackGraphService(session.deps.graph_store).mark_cleaned(
            item.node_id, evidence_id=item.created_evidence_id,
        )
        _, _, _, errors, *_ = await goal_inputs(session.deps, session.runtime)
        assert "cleanup_original_effect_unproven" in errors


async def test_readonly_projection_preserves_db_and_shows_live_waits(tmp_path):
    async with selection(tmp_path, tools=OriginalProtocolDouble(gateway(risk="high"))) \
            as (session, llm):
        original, = await start(session, llm)
        path = session.deps.central_state.path
        with sqlite3.connect(path) as db:
            before = list(db.iterdump())
        _, _, report = await inspect_central_run(path, str(original.run_id))
        assert report.phase is AgentPhase.OBSERVE
        assert report.executions[0]["approval_ref"] == original.pending_approval_id
        assert not report.ready_for_release
        with sqlite3.connect(path) as db:
            assert list(db.iterdump()) == before


async def test_confirmed_cleanup_uses_same_resource_and_preserved_originals(tmp_path):
    inner = gateway()
    for index, kind in enumerate(("created", "cleaned", "created")):
        inner.responses[index] = DevelopmentResponse(
            output={"resource_id": "object-a", "confirmed": True},
            metadata={"resource_effects": [{"version": 2, "identity_domain": "synthetic-backend",
                                           "effect": kind, "resource_type": "object",
                                           "identity_path": ["resource_id"],
                                           "confirmation_path": ["confirmed"]}]},
        )
    async with selection(tmp_path, tools=OriginalProtocolDouble(inner)) as (session, llm):
        await finish_inconclusive(session, llm)
        graph = AttackGraphService(session.deps.graph_store)
        created, = (await graph.cleanup_report()).items
        await finish_inconclusive(session, llm)
        cleaned, = (await graph.cleanup_report()).items
        assert cleaned.node_id == created.node_id
        assert not cleaned.is_outstanding and cleaned.cleanup_evidence_id
        assert cleaned.created_evidence_id != cleaned.cleanup_evidence_id
        _, _, _, errors, *_ = await goal_inputs(session.deps, session.runtime)
        assert errors == []
        await finish_inconclusive(session, llm)
        recreated, = (await graph.cleanup_report()).items
        assert recreated.is_outstanding and recreated.cleanup_evidence_id is None
        assert recreated.created_evidence_id != created.created_evidence_id


async def test_owned_resume_runs_original_zero_entry_actor(tmp_path):
    from redteam.core.ports.llm import NativeCompletion
    from redteam.orchestration.central_runtime import ready_workers
    from tests.integration.test_central_development import TOOL, discover, inspect, register, turn

    async with selection(tmp_path) as (session, llm):
        task_id, = await register(session)
        original, = await ready_workers(session.deps, session.runtime)
        llm.native_responses_by_task[task_id] = [
            discover(TOOL), turn(inspect("same-original-instance")),
            NativeCompletion(text="Further investigation needed", total_tokens=1),
        ]
        llm._canned_structured = {
            "questions": [], "termination": "held",
            "resume_conditions": ["A new result or operator instruction arrives"],
            "reason": "No further work proposed",
        }
        final = await session.resume()
        assert final.run_id == original.run_id
        execution, = await session.deps.central_state.list_executions()
        assert execution.instance_id == original.instance_id
        assert final.termination_reason == TerminationReason.INVESTIGATION_HELD


async def test_followup_registration_is_not_question_resolution(tmp_path):
    from redteam.core.contracts.completion import completion_conditions
    from redteam.core.ports.llm import NativeCompletion
    from redteam.orchestration.central_runtime import ready_workers
    from tests.integration.test_central_development import (
        TOOL,
        discover,
        inspect,
        proposal,
        register,
        turn,
    )

    async with selection(tmp_path) as (session, llm):
        await finish_inconclusive(session, llm, questions=[{
            "question_id": "needed", "question": "Verify original response",
            "required_for_goal": True, "reason": "Necessary to evaluate the criterion",
        }])
        state = session.deps.central_state
        source, = await state.list_tasks()
        followup, = await register(session, "followup-registration", proposal(
            source_task_id=str(source.task_id), source_result_id=source.result.result_id,
            source_question_id="needed",
        ))
        assert "goal_required_questions_unresolved" in (
            await goal_inputs(session.deps, session.runtime)
        )[3]
        llm.native_responses_by_task[followup] = [
            discover(TOOL), turn(inspect("followup-call")),
            NativeCompletion(text="Original response inspected", total_tokens=1),
        ]
        workers = await ready_workers(session.deps, session.runtime)
        outcomes = [item async for item in session.driver.drive(workers)]
        runtime = outcomes[0].runtime.model_copy(update={"phase": AgentPhase.OBSERVE})
        records = await session.deps.evidence_store.list_for_run(runtime.run_id)
        refs = [str(record.evidence_id) for record in records
                if isinstance(record.metadata.get("tool_result"), dict)
                and record.metadata.get("mission_task_id") == followup]
        task = await state.get_task(followup)
        llm._canned_structured = {
            "accepted": True, "reason": "Actual followup originals assessed",
            "claims": [{"kind": "semantic", "description": "Response verified",
                        "evidence_ids": refs}],
            "criterion_results": [{"criterion_id": condition["criterion_id"], "met": True,
                                   "reason": "Original inspected", "evidence_ids": refs}
                                  for condition in completion_conditions(task)],
        }
        from redteam.orchestration.metered_llm import llm_request_scope
        with llm_request_scope(str(runtime.run_id), runtime.work_owner):
            settled = await settle_central(
                "task", CompletionProposal(outcome="success", reason="Followup reviewed",
                                           evidence_ids=refs),
                LocalToolContext(session.deps, runtime),
            )
        assert settled.ok, settled.error
        assert (await goal_inputs(session.deps, session.runtime))[3] == []


async def test_goal_validates_receipt_for_followup_adopting_original_source(tmp_path):
    """Task B can independently review Task A's original and carry it to the goal."""
    from redteam.core.contracts.completion import criterion_id
    from redteam.orchestration.central_runtime import ready_workers
    from tests.integration.test_central_development import proposal, register

    async with selection(tmp_path) as (session, llm):
        original_refs = await finish_inconclusive(session, llm, questions=[{
            "question_id": "required",
            "question": "Independently judge whether the original response proves the goal",
            "required_for_goal": True,
            "reason": "A second task must assess relevance to the goal",
        }])
        state = session.deps.central_state
        source, = await state.list_tasks()
        candidate = proposal(
            source_task_id=str(source.task_id),
            source_result_id=source.result.result_id,
            source_question_id="required",
        )
        followup_proposal = candidate.model_copy(update={
            "tasks": [candidate.tasks[0].model_copy(update={
                "objective": "Judge whether the admin path reuses the observed session",
            })],
        })
        followup_id, = await register(
            session, "followup-adopts-source-original", followup_proposal,
        )
        worker, = await ready_workers(session.deps, session.runtime)
        task = await state.get_task(followup_id)
        llm._canned_structured = {
            "accepted": True,
            "reason": "The source response independently supports the follow-up purpose",
            "claims": [{
                "kind": "semantic",
                "description": "The preserved source response supports the task purpose",
                "evidence_ids": original_refs,
            }],
            "criterion_results": [{
                "criterion_id": criterion_id(condition),
                "met": True,
                "reason": "The source response supports this fixed condition",
                "evidence_ids": original_refs,
            } for condition in task.completion_criteria],
        }
        from redteam.orchestration.central_results import settle_central
        from redteam.orchestration.completion_proposal import CompletionProposal
        from redteam.orchestration.local_tools import LocalToolContext

        settled = await settle_central(
            "task",
            CompletionProposal(
                outcome="success", reason="Independently reviewed source original",
                evidence_ids=original_refs,
            ),
            LocalToolContext(
                session.deps, worker.model_copy(update={"phase": AgentPhase.OBSERVE}),
            ),
        )
        assert settled.ok, settled.error
        await state.transaction(lambda tx: tx.actor_returned(
            followup_id,
            owner=worker.work_owner,
            version=worker.task_ownership_version,
            instance_id=str(worker.instance_id),
        ))
        completed = await state.get_task(followup_id)
        assert completed.result.outcome == "confirmed"
        assert completed.result.completion_receipt_id is not None
        receipt = await session.deps.evidence_store.get(
            str(completed.result.completion_receipt_id),
        )
        assert receipt.metadata["task_id"] == followup_id
        assert receipt.metadata["source_evidence_ids"] == original_refs

        inputs = await goal_inputs(session.deps, session.runtime)
        assert inputs[3] == []
        assert inputs[4][followup_id] == original_refs
        assert await review(session, llm, original_refs) == []
        mission = await state.get_mission(session.runtime.mission_id)
        assert mission.status is MissionStatus.COMPLETED


async def test_exhaustion_is_a_fresh_judgment_and_never_goal_success(tmp_path):
    from redteam.orchestration.specialist_planner import SpecialistPlanner
    async with selection(tmp_path) as (session, llm):
        refs = await finish_inconclusive(session, llm)
        llm._canned_structured = {
            "questions": [], "termination": "exhausted",
            "resume_conditions": ["A relevant new result changes the available evidence"],
            "reason": "Original response assessed, no useful followup",
        }
        await SpecialistPlanner(session.deps).advance(session.runtime)
        mission = await session.deps.central_state.get_mission(session.runtime.mission_id)
        llm._canned_structured = {
            "accepted": False, "reason": "Goal criterion remains unmet",
            "criteria": [{"criterion": c, "met": False, "evidence_ids": refs, "claims": []}
                         for c in mission.success_criteria],
        }
        final = await session.settle(session.runtime)
        assert final.termination_reason == TerminationReason.EXHAUSTED
        assert final.mission_status is MissionStatus.ACTIVE


async def test_exhaustion_does_not_replace_an_external_task_hold(tmp_path):
    from redteam.orchestration.specialist_planner import SpecialistPlanner
    from tests.integration.test_central_development import register

    async with selection(tmp_path) as (session, llm):
        await finish_inconclusive(session, llm)
        blocked_id, = await register(session, "external-hold")
        await session.deps.central_state.transaction(lambda tx: tx._write_task(
            tx.get_task(str(blocked_id)).model_copy(update={
                "required": False,
                "status": TaskStatus.BLOCKED,
                "blocking_reason": "awaiting_external_prerequisite",
            }),
        ))
        llm._canned_structured = {
            "questions": [], "termination": "exhausted",
            "resume_conditions": ["The external prerequisite becomes available"],
            "reason": "No useful authorized followup is currently available",
        }
        await SpecialistPlanner(session.deps).advance(session.runtime)
        llm._canned_structured = {
            "accepted": False, "reason": "Goal criterion remains unmet",
            "criteria": [{"criterion": "Assess the original response", "met": False,
                          "evidence_ids": [], "claims": []}],
        }

        final = await session.settle(session.runtime)

        assert final.termination_reason == TerminationReason.INVESTIGATION_HELD
        assert final.mission_status is MissionStatus.ACTIVE
        blocked = await session.deps.central_state.get_task(str(blocked_id))
        assert blocked.status is TaskStatus.BLOCKED
        assert blocked.blocking_reason == "awaiting_external_prerequisite"


async def test_background_reasoning_drains_under_same_driver_ownership(tmp_path):
    async with selection(tmp_path) as (session, _):
        waiting = asyncio.create_task(asyncio.Event().wait())
        session.driver.track_background(waiting)
        await session.driver.close()
        assert waiting.cancelled()


async def test_final_review_rechecks_required_original_not_cited_by_criterion(tmp_path):
    async with selection(tmp_path) as (session, llm):
        first = await finish_inconclusive(session, llm)
        second = await finish_inconclusive(session, llm)
        before = len(llm.requests)
        llm.request_release.clear()
        pending = asyncio.create_task(review(session, llm, second))
        await asyncio.wait_for(llm.wait_for_requests(before + 1), 3)
        path = tmp_path / "run" / "evidence" / f"{session.runtime.run_id}.jsonl"
        lines = path.read_text().splitlines()
        path.write_text("\n".join(line for line in lines
                                  if json.loads(line)["evidence_id"] != first[0]) + "\n")
        llm.request_release.set()
        with pytest.raises(StateConflict, match="evidence_file_index_disagreement"):
            await pending
        assert (await session.deps.central_state.get_mission(
            session.runtime.mission_id,
        )).status is MissionStatus.ACTIVE
