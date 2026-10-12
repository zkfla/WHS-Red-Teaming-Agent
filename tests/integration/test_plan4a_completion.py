"""4a originals/SQL/real settlement paths; explicit provider/LLM doubles only."""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from redteam.attack_graph.predicates import Confidence, Provenance
from redteam.attack_graph.service import AttackGraphService
from redteam.capability.development import DevelopmentResponse, DevelopmentToolGateway
from redteam.core.contracts.completion import criterion_id
from redteam.core.contracts.tool import ToolResult
from redteam.core.enums import AgentPhase, GraphNodeType, MissionStatus, TaskStatus
from redteam.core.ids import GraphNodeId
from redteam.core.ports.llm import NativeCompletion
from redteam.orchestration.central_completion import goal_inputs
from redteam.orchestration.central_results import settle_central
from redteam.orchestration.central_runtime import ready_workers
from redteam.orchestration.completion_proposal import CompletionProposal
from redteam.orchestration.coordination import StateConflict, content_hash
from redteam.orchestration.engine import InvestigationEngine
from redteam.orchestration.evidence_validity import assess, capture_validity
from redteam.orchestration.local_tools import LocalToolContext
from redteam.orchestration.resource_cleanup import apply_resource_effects
from redteam.orchestration.semantic_completion import complete_task
from tests.integration.test_central_completion import (
    OriginalProtocolDouble,
    finish_inconclusive,
    review,
    selection,
)
from tests.integration.test_central_development import (
    HOST,
    TARGET,
    TOOL,
    call,
    discover,
    gateway,
    register,
    turn,
)
from tests.integration.test_central_recovery import start


class Clock(datetime):
    value = datetime.now(timezone.utc)

    @classmethod
    def now(cls, tz=None):
        return cls.value


def observed_tools():
    inner = gateway()
    for response in inner.responses:
        response.output.update(
            observed_deadline=(Clock.value + timedelta(seconds=5)).isoformat(),
            observed_version="original",
        )
    return OriginalProtocolDouble(inner)


async def originals_for(session, runtime):
    return [str(record.evidence_id)
            for record in await session.deps.evidence_store.list_for_run(runtime.run_id)
            if isinstance(record.metadata.get("tool_result"), dict)
            and record.metadata.get("mission_task_id") == str(runtime.active_task_id)]


async def bind(session, runtime, originals, *, expiry=False):
    graph = AttackGraphService(session.deps.graph_store)
    await graph.upsert_node(
        GraphNodeType.HOST, node_id=GraphNodeId(TARGET),
        facts={"observed_deadline": (Clock.value + timedelta(seconds=5)).isoformat(),
               "observed_version": "original"},
        provenance=Provenance(run_id=str(runtime.run_id), evidence_id=originals[0],
                              confidence=Confidence.MEASURED),
    )
    conditions = [{"node_id": TARGET, "container": "fact",
                   "key": "observed_deadline" if expiry else "observed_version",
                   "test": "not_expired" if expiry else "unchanged"}]
    result = await assess({"evidence_id": originals[0], "conditions": conditions},
                          LocalToolContext(session.deps, runtime))
    assert result.ok, result.error
    return conditions


async def task_review(session, llm, runtime, originals, *, entry="native"):
    task = await session.deps.central_state.get_task(str(runtime.active_task_id))
    llm._canned_structured = {
        "accepted": True, "reason": "Original response assessed",
        "claims": [{"kind": "semantic", "description": "Original response assessed",
                    "evidence_ids": originals}],
        "criterion_results": [{"criterion_id": criterion_id(condition), "met": True,
                               "evidence_ids": originals, "reason": "Response assessed"}
                              for condition in task.completion_criteria],
    }
    proposal = CompletionProposal(outcome="success", reason="Response assessed",
                                  evidence_ids=originals)
    resumed = runtime.model_copy(update={"phase": AgentPhase.OBSERVE})
    if entry == "public":
        return await complete_task(
            InvestigationEngine(session.deps), None,
            SimpleNamespace(completion=proposal.model_dump(mode="json"), reason=proposal.reason),
            resumed, {},
        )
    return await settle_central("task", proposal, LocalToolContext(session.deps, resumed))


async def test_public_positive_review_uses_original_run_meter(tmp_path):
    async with selection(tmp_path) as (session, llm):
        runtime, = await start(session, llm)
        originals = await originals_for(session, runtime)
        requests = len(llm.requests)
        assert await task_review(session, llm, runtime, originals, entry="public") == []
        assert len(llm.requests) == requests + 1
        task = await session.deps.central_state.get_task(str(runtime.active_task_id))
        assert task.status is TaskStatus.COMPLETED and task.result.completion_receipt_id


@pytest.mark.parametrize("scope", ["task", "goal"])
@pytest.mark.parametrize("change", ["expiry", "condition", "new_binding", "unrelated"])
async def test_review_rechecks_only_related_support(tmp_path, monkeypatch, scope, change):
    monkeypatch.setattr("redteam.orchestration.evidence_validity.datetime", Clock)
    async with selection(tmp_path, tools=observed_tools()) as (session, llm):
        if scope == "goal":
            originals = await finish_inconclusive(session, llm)
            runtime = session.runtime
        else:
            runtime, = await start(session, llm)
            originals = await originals_for(session, runtime)
        await bind(session, runtime, originals, expiry=change == "expiry")
        requests = len(llm.requests)
        llm.request_release.clear()
        pending = asyncio.create_task(
            review(session, llm, originals) if scope == "goal"
            else task_review(session, llm, runtime, originals),
        )
        try:
            await asyncio.wait_for(llm.wait_for_requests(requests + 1), 5)
            graph = AttackGraphService(session.deps.graph_store)
            if change == "expiry":
                monkeypatch.setattr(Clock, "value", Clock.value + timedelta(seconds=10))
            elif change == "condition":
                await graph.upsert_node(GraphNodeType.HOST, node_id=GraphNodeId(TARGET),
                                        facts={"observed_version": "changed"})
            elif change == "new_binding":
                # Direct append models historical/public evidence writers too.
                from redteam.core.contracts.evidence import EvidenceRecord
                from redteam.core.enums import EvidenceSourceType
                from redteam.orchestration.evidence_validity import SOURCE_ID, ReuseBinding

                source = await session.deps.evidence_store.get(originals[0])
                metadata = ReuseBinding(
                    claim_evidence_id=originals[0], source_hash=content_hash(source),
                    conditions=[{"node_id": TARGET, "container": "fact",
                                 "key": "observed_deadline"}],
                    values={f"{TARGET}#fact:observed_deadline": content_hash(
                        (Clock.value + timedelta(seconds=5)).isoformat(),
                    )},
                ).model_dump(mode="json")
                await session.deps.evidence_store.append(EvidenceRecord(
                    evidence_id="new-related-binding", run_id=runtime.run_id,
                    engagement_id=runtime.engagement_id, source_type=EvidenceSourceType.SYSTEM,
                    source_id=SOURCE_ID, digest=content_hash(metadata), metadata=metadata,
                    content_type="application/json", created_at=Clock.value,
                ))
            else:
                await graph.upsert_node(GraphNodeType.HOST, node_id=GraphNodeId(TARGET),
                                        facts={"unrelated_fact": "later observation"})
        finally:
            llm.request_release.set()
        try:
            outcome = await asyncio.wait_for(pending, 5)
        except StateConflict:
            assert change != "unrelated"
        else:
            passed = outcome == [] if scope == "goal" else outcome.ok
            assert passed == (change == "unrelated"), outcome
        mission = await session.deps.central_state.get_mission(runtime.mission_id)
        task = (await session.deps.central_state.get_task(str(runtime.active_task_id))) \
            if scope == "task" else None
        if change != "unrelated":
            assert mission.status is MissionStatus.ACTIVE
            if task is not None:
                assert task.status is TaskStatus.RUNNING


@pytest.mark.parametrize("outcome", ["inconclusive", "blocked"])
@pytest.mark.parametrize("entry", ["public", "native"])
async def test_unresolved_entrypoints_share_central_result(tmp_path, outcome, entry):
    async with selection(tmp_path) as (session, llm):
        runtime, = await start(session, llm)
        await register(session, "untouched-sibling")
        sibling, = await ready_workers(session.deps, session.runtime)
        originals = await originals_for(session, runtime)
        proposal = CompletionProposal(outcome=outcome, reason="Recorded uncertainty",
                                      evidence_ids=originals, gaps=[{
                                          "description": "Follow up later",
                                          "unresolved_questions": ["What explains this response?"],
                                          "evidence_ids": originals,
                                      }])
        requests = len(llm.requests)
        if entry == "public":
            errors = await complete_task(
                InvestigationEngine(session.deps), None,
                SimpleNamespace(completion=proposal.model_dump(mode="json"),
                                reason=proposal.reason),
                runtime, {},
            )
            assert errors == []
        else:
            result = await settle_central("task", proposal, LocalToolContext(session.deps, runtime))
            assert result.ok, result.error
        task = await session.deps.central_state.get_task(str(runtime.active_task_id))
        other = await session.deps.central_state.get_task(str(sibling.active_task_id))
        assert task.status is TaskStatus.COMPLETED
        assert task.result.outcome == outcome and task.result.questions
        assert task.result.completion_receipt_id is None
        assert other.status is TaskStatus.RUNNING
        assert len(llm.requests) == requests


async def test_resource_domains_survive_native_create_delete_and_goal(tmp_path):
    names = ["service_a.create", "service_b.create", "service_b.remove", "service_a.remove"]
    base = gateway().catalog.get(TOOL)
    descriptors = [base.model_copy(update={"tool_id": name, "server_id": name.split(".")[0],
                                           "tool_name": name.split(".")[1]}) for name in names]
    responses = [DevelopmentResponse(
        output={"id": 1, "confirmed": True},
        metadata={"resource_effects": [{"version": 2, "identity_domain": name.split(".")[0],
                                       "effect": "created" if index < 2 else "cleaned",
                                       "resource_type": "account", "identity_path": ["id"],
                                       "confirmation_path": ["confirmed"]}]},
    ) for index, name in enumerate(names)]
    tools = OriginalProtocolDouble(DevelopmentToolGateway(descriptors, responses))
    async with selection(tmp_path, tools=tools) as (session, llm):
        all_originals, counts = [], []
        for index, name in enumerate(names):
            identity, = await register(session, f"resource-operation-{index}")
            llm.native_responses_by_task[identity] = [
                discover(name), turn(call(f"call-{index}", name, {"host": HOST, "account": "0"})),
                NativeCompletion(text="Observed resource effect", total_tokens=1),
            ]
            workers = await ready_workers(session.deps, session.runtime)
            async for result in session.driver.drive(workers):
                assert result.kind == "completed", result.error
                runtime = result.runtime
            originals = await originals_for(session, runtime)
            settled = await settle_central(
                "task", CompletionProposal(outcome="inconclusive", reason="Effect observed",
                                           evidence_ids=originals),
                LocalToolContext(session.deps, runtime),
            )
            assert settled.ok, settled.error
            all_originals.extend(originals)
            report = await AttackGraphService(session.deps.graph_store).cleanup_report()
            counts.append(len(report.outstanding))
            if index == 2:
                assert "required_cleanup_outstanding" in await review(session, llm, all_originals)
                await session.deps.central_state.transaction(
                    lambda tx: tx.set_settlement_gate(False),
                )
        assert counts == [1, 2, 1, 0]
        assert await review(session, llm, all_originals) == []


async def test_resource_replay_wrong_domain_and_older_removal(tmp_path):
    """Direct effect consumer checks; native original/goal linkage is tested above."""
    async with selection(tmp_path) as (session, _):
        service = AttackGraphService(session.deps.graph_store)
        effect = {"version": 2, "identity_domain": "backend-a", "effect": "created",
                  "resource_type": "account", "identity_path": ["id"],
                  "confirmation_path": ["confirmed"]}
        now = datetime.now(timezone.utc)
        created = ToolResult(
            invocation_id="synthetic-invocation", action_id="synthetic-action", tool_id=TOOL,
            success=True, started_at=now, finished_at=now, output={"id": 1, "confirmed": True},
            metadata={"resource_effects": [effect]},
        )
        for _ in range(2):
            await apply_resource_effects(service, result=created, runtime=session.runtime,
                                         evidence_id="synthetic-create-reference")
        assert len((await service.cleanup_report()).outstanding) == 1
        for case, message in [("domain", "resource_missing"), ("generation", "generation_unproven"),
                              ("legacy", "domain_unproven")]:
            removal_effect = {**effect, "effect": "cleaned"}
            if case == "domain":
                removal_effect["identity_domain"] = "backend-b"
            if case == "legacy":
                removal_effect.pop("identity_domain")
                removal_effect["version"] = 1
            result = created.model_copy(update={
                "metadata": {"resource_effects": [removal_effect]},
                "started_at": now - timedelta(seconds=1) if case == "generation" else now,
            })
            with pytest.raises(StateConflict, match=message):
                await apply_resource_effects(service, result=result, runtime=session.runtime,
                                             evidence_id="synthetic-removal-reference")
            assert len((await service.cleanup_report()).outstanding) == 1


@pytest.mark.parametrize("related", [True, False])
async def test_claim_graph_proof_checked_at_actual_task_commit(tmp_path, monkeypatch, related):
    from redteam.orchestration import central_results

    async with selection(tmp_path) as (session, llm):
        runtime, = await start(session, llm)
        originals = await originals_for(session, runtime)
        original_commit = central_results.commit_result

        async def race(*args, **kwargs):
            if kwargs.get("graph_proof"):
                await AttackGraphService(session.deps.graph_store).upsert_node(
                    GraphNodeType.HOST,
                    node_id=GraphNodeId(TARGET if related else "host:unrelated-proof"),
                    facts={"later_observation": "changed after proof validation"},
                )
            return await original_commit(*args, **kwargs)

        monkeypatch.setattr(central_results, "commit_result", race)
        # Keep the response scripted while exercising actual review/receipt/SQL.
        task = await session.deps.central_state.get_task(str(runtime.active_task_id))
        llm._canned_structured = {
            "accepted": True, "reason": "Target response assessed",
            "claims": [{"kind": "semantic", "description": "Target response assessed",
                        "evidence_ids": originals, "target_node_ids": [TARGET]}],
            "criterion_results": [{"criterion_id": criterion_id(item), "met": True,
                                   "evidence_ids": originals, "reason": "Target assessed"}
                                  for item in task.completion_criteria],
        }
        proposal = CompletionProposal(outcome="success", reason="Target assessed",
                                      evidence_ids=originals)
        context = LocalToolContext(session.deps, runtime.model_copy(update={
            "phase": AgentPhase.OBSERVE,
        }))
        if related:
            with pytest.raises(StateConflict, match="graph_proof_changed"):
                await settle_central("task", proposal, context)
        else:
            assert (await settle_central("task", proposal, context)).ok
        current = await session.deps.central_state.get_task(str(runtime.active_task_id))
        assert (current.status is TaskStatus.COMPLETED) == (not related)


@pytest.mark.parametrize("case", ["before_review", "unused_during_review", "used_during_review"])
async def test_optional_expired_support_does_not_block_independent_goal(
    tmp_path, monkeypatch, case,
):
    monkeypatch.setattr("redteam.orchestration.evidence_validity.datetime", Clock)
    async with selection(tmp_path, tools=observed_tools()) as (session, llm):
        primary = await finish_inconclusive(session, llm)
        runtime, = await start(session, llm, registration_prefix="optional")
        task = await session.deps.central_state.get_task(str(runtime.active_task_id))
        await session.deps.central_state.transaction(lambda tx: tx.update_task(
            task.model_copy(update={"required": False}), owner=runtime.work_owner,
            version=runtime.task_ownership_version, expected_hash=content_hash(task),
        ))
        originals = await originals_for(session, runtime)
        await bind(session, runtime, originals, expiry=True)
        result = await settle_central(
            "task", CompletionProposal(outcome="inconclusive", reason="Optional response observed",
                                       evidence_ids=originals),
            LocalToolContext(session.deps, runtime),
        )
        assert result.ok, result.error
        if case == "before_review":
            monkeypatch.setattr(Clock, "value", Clock.value + timedelta(seconds=10))
        inputs = await goal_inputs(session.deps, session.runtime)
        assert inputs[3] == []
        assert (str(runtime.active_task_id) not in inputs[4]) == (case == "before_review")
        selected = originals if case == "used_during_review" else primary
        if case == "before_review":
            assert await review(session, llm, selected) == []
        else:
            requests = len(llm.requests)
            llm.request_release.clear()
            pending = asyncio.create_task(review(session, llm, selected))
            try:
                await asyncio.wait_for(llm.wait_for_requests(requests + 1), 5)
                monkeypatch.setattr(Clock, "value", Clock.value + timedelta(seconds=10))
            finally:
                llm.request_release.set()
            errors = await pending
            assert bool(errors) == (case == "used_during_review")


@pytest.mark.parametrize("change", ["expiry", "new_binding", "condition", "unrelated"])
async def test_final_sql_checks_time_and_new_binding_reference(tmp_path, monkeypatch, change):
    monkeypatch.setattr("redteam.orchestration.evidence_validity.datetime", Clock)
    async with selection(tmp_path, tools=observed_tools()) as (session, llm):
        originals = await finish_inconclusive(session, llm)
        await bind(session, session.runtime, originals, expiry=True)
        read, errors = await capture_validity(session.deps, originals)
        assert not errors
        if change == "expiry":
            monkeypatch.setattr(Clock, "value", Clock.value + timedelta(seconds=10))
        elif change == "new_binding":
            result = await assess({"evidence_id": originals[0], "conditions": [{
                "node_id": TARGET, "container": "fact", "key": "observed_version",
            }]}, LocalToolContext(session.deps, session.runtime))
            assert result.ok, result.error
        else:
            await AttackGraphService(session.deps.graph_store).upsert_node(
                GraphNodeType.HOST, node_id=GraphNodeId(TARGET),
                facts={"observed_deadline" if change == "condition" else "unrelated": "changed"},
            )
        if change == "unrelated":
            await session.deps.central_state.transaction(read.check)
        else:
            with pytest.raises(StateConflict, match="reassessment_required"):
                await session.deps.central_state.transaction(read.check)


async def test_binding_append_failure_retains_reference_fence(tmp_path, monkeypatch):
    async with selection(tmp_path, tools=observed_tools()) as (session, llm):
        originals = await finish_inconclusive(session, llm)
        await AttackGraphService(session.deps.graph_store).upsert_node(
            GraphNodeType.HOST, node_id=GraphNodeId(TARGET), facts={"observed_version": "original"},
            provenance=Provenance(run_id=str(session.runtime.run_id), evidence_id=originals[0],
                                  confidence=Confidence.MEASURED),
        )
        read, errors = await capture_validity(session.deps, originals)
        assert not errors

        async def lost_append(*args, **kwargs):
            raise OSError("Synthetic lost evidence append")

        monkeypatch.setattr(session.deps.evidence_store, "append", lost_append)
        result = await assess({"evidence_id": originals[0], "conditions": [{
            "node_id": TARGET, "container": "fact", "key": "observed_version",
        }]}, LocalToolContext(session.deps, session.runtime))
        assert not result.ok
        with pytest.raises(StateConflict, match="binding_changed"):
            await session.deps.central_state.transaction(read.check)
        with pytest.raises(StateConflict, match="binding_original_unavailable"):
            await capture_validity(session.deps, originals)
