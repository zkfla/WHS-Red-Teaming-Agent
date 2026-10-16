"""Real result handlers/SQLite with explicit synthetic originals; no target traffic."""

from datetime import datetime, timezone

import pytest

from redteam.attack_graph.service import AttackGraphService
from redteam.core.contracts.action import ActionProposal
from redteam.core.contracts.assessment import AssessmentDraft
from redteam.core.contracts.evidence import EvidenceRecord
from redteam.core.contracts.mission import InvestigationResult
from redteam.core.contracts.tool import ToolResult
from redteam.core.convert import tool_result_to_evidence
from redteam.core.enums import AgentPhase, TaskStatus
from redteam.llm.fake import FakeLLMClient
from redteam.orchestration.central_results import publish_result_handler, settle_central
from redteam.orchestration.central_runtime import ready_workers
from redteam.orchestration.central_state import CentralStateStore
from redteam.orchestration.completion_proposal import CompletionProposal
from redteam.orchestration.coordination import StateConflict, content_hash
from redteam.orchestration.local_tools import LocalToolContext
from redteam.orchestration.probes import yield_probe
from tests.fakes.assessment_drafts import draft
from tests.fakes.worker_input import worker_input
from tests.integration.test_central_development import (
    HOST,
    TARGET,
    TOOL,
    call,
    discover,
    inspect,
    turn,
)
from tests.integration.test_probe_workers import (
    ReviewedObservationModel,
    drive,
    register,
    selection,
)


async def owned_original(session, *, candidate=False):
    """Seed a storage-only source, not an actual completed execution or a success receipt."""
    updates = [{"expected_revision": None, "hypothesis": {
        "contract_version": 2, "hypothesis_id": "candidate", "description": "Unreviewed premise",
        "target_node_ids": [TARGET], "reason": "Keep a proposal separate from a verdict",
    }}] if candidate else []
    identity = await register(session, "assessment-storage", actions=2,
                              hypotheses={"candidate": 1} if candidate else {}, updates=updates)
    runtime, = await ready_workers(session.deps, session.runtime)
    runtime = runtime.model_copy(update={"phase": AgentPhase.OBSERVE})
    now = datetime.now(timezone.utc)
    proposal = ActionProposal.model_validate({
        "action_id": "action-assessment", "run_id": runtime.run_id,
        "engagement_id": runtime.engagement_id, "agent_id": runtime.current_agent,
        "binding_mode": "selected_tool", "tool_id": TOOL,
        "target": {"kind": "host", "host": HOST}, "arguments": {"host": HOST},
        "reason": "Synthetic source for a storage contract test", "created_at": now,
    })
    raw = ToolResult.model_validate({
        "action_id": proposal.action_id, "invocation_id": "inv-assessment", "tool_id": TOOL,
        "success": True, "output": {"status": 200, "body_preview": "Retained safe text."},
        "started_at": now, "finished_at": now,
    })
    record = tool_result_to_evidence(raw, run_id=runtime.run_id,
                                     engagement_id=runtime.engagement_id)
    record.metadata.update({
        "tool_result": raw.model_dump(mode="json"), "proposal": proposal.model_dump(mode="json"),
        "target_node_id": TARGET, "tool_id": TOOL,
    })
    await session.deps.evidence_store.append(record)
    return identity, runtime, record


def publication(drafts, *, result_id="result-assessment"):
    return {"result": {"result_id": result_id, "outcome": "inconclusive",
                       "summary": "Store assertions without deciding their meaning",
                       "assessment_drafts": drafts}}


@pytest.mark.parametrize("outcome", ["inconclusive", "blocked"])
@pytest.mark.parametrize("intermediate", [False, True])
async def test_direct_or_published_settlement_retains_drafts_and_reopens(
    tmp_path, outcome, intermediate,
):
    async with selection(tmp_path) as (session, llm, tools):
        identity, runtime, original = await owned_original(session)
        context = LocalToolContext(session.deps, runtime)
        payload = draft(target=TARGET, evidence_id=str(original.evidence_id),
                        judgment="supports", severity="high",
                        reason="Worker proposal password=StorageSentinel")
        if intermediate:
            first = await publish_result_handler(publication([payload]), context)
            assert first.ok, first.error
            before = await session.deps.central_state.get_task(identity)
            cursor = await session.deps.central_state.transaction(
                lambda tx: tx.latest_event_sequence(["InvestigationResultUpdated"]), write=False,
            )
            replay = await publish_result_handler(publication([payload]), context)
            assert replay.ok
            replayed = await session.deps.central_state.get_task(identity)
            assert replayed.updated_at == before.updated_at
            assert await session.deps.central_state.transaction(
                lambda tx: tx.latest_event_sequence(["InvestigationResultUpdated"]), write=False,
            ) == cursor
        proposal = CompletionProposal.model_validate({
            "outcome": outcome, "reason": "Unresolved meaning; preserve the assertion",
            "assessment_drafts": [] if intermediate else [payload],
            "observations": ["The stored field is present."],
            "limitations": ["The security meaning is not independently reviewed."],
        })
        response = await settle_central("task", proposal, context)
        assert response.ok, response.error
        saved = await session.deps.central_state.get_task(identity)
        assert saved.status is TaskStatus.COMPLETED
        assert saved.result.outcome == outcome
        assert saved.result.observations == proposal.observations
        assert saved.result.limitations == proposal.limitations
        expected = AssessmentDraft.model_validate(payload)
        assert saved.result.assessment_drafts == [expected]
        assert "StorageSentinel" not in saved.model_dump_json()
        assert not llm.requests and not tools.inner.invocations
        assert await AttackGraphService(session.deps.graph_store).list_findings() == []
        assert await session.deps.central_state.candidate_finding_promotions() == []
        # A new storage reader uses the same DB; no current-machine data is backfilled.
        reopened = CentralStateStore(
            session.deps.central_state.path,
            run_id=str(runtime.run_id), engagement_id=str(runtime.engagement_id),
        )
        restored = await reopened.get_task(identity)
        assert restored.result == saved.result
        assert restored.result.assessment_drafts[0].identity(saved.result.result_id) == (
            expected.identity(saved.result.result_id)
        )


@pytest.mark.parametrize("breach", [
    "missing_path", "nontext_span", "span_bounds", "foreign_run", "foreign_engagement",
    "missing_source", "target", "principal", "hypothesis", "source_target", "diagnostic",
])
async def test_invalid_draft_reference_is_rejected_without_result_or_gap_write(tmp_path, breach):
    async with selection(tmp_path) as (session, llm, tools):
        identity, runtime, original = await owned_original(session)
        payload = draft(target=TARGET, evidence_id=str(original.evidence_id))
        pointer = payload["observations"][0]["sources"][0]
        if breach == "missing_path":
            pointer["raw_path"] = ["unavailable"]
        elif breach == "nontext_span":
            pointer["text_span"] = {"offset": 0, "length": 1}
        elif breach == "span_bounds":
            pointer.update(raw_path=["body_preview"], text_span={"offset": 10, "length": 999})
        elif breach in {"foreign_run", "foreign_engagement", "source_target", "diagnostic"}:
            updates = {"evidence_id": "ev-invalid"}
            if breach == "foreign_run":
                updates["run_id"] = "run-other"
            elif breach == "foreign_engagement":
                updates["engagement_id"] = "eng-other"
            elif breach == "source_target":
                updates["metadata"] = {**original.metadata, "target_node_id": "host:other.local"}
            else:
                updates.update(source_type="system", source_id="orchestration.investigation_result",
                               metadata={"record_kind": "investigation_unresolved"})
            changed = EvidenceRecord.model_validate({**original.model_dump(), **updates})
            await session.deps.evidence_store.append(changed)
            payload = draft(target=TARGET, evidence_id=str(changed.evidence_id))
        elif breach == "missing_source":
            payload = draft(target=TARGET, evidence_id="ev-missing")
        elif breach == "target":
            payload["target_node_id"] = "host:outside.local"
        elif breach == "principal":
            payload["principal_refs"] = ["principal-unassigned"]
        elif breach == "hypothesis":
            payload["hypothesis_id"] = "hypothesis-unassigned"
        records_before = await session.deps.evidence_store.list_for_run(runtime.run_id)
        with pytest.raises(StateConflict):
            await settle_central("task", CompletionProposal.model_validate({
                "outcome": "inconclusive", "reason": "Preserve a proposal",
                "assessment_drafts": [payload],
            }), LocalToolContext(session.deps, runtime))
        assert (await session.deps.central_state.get_task(identity)).result is None
        assert await session.deps.evidence_store.list_for_run(runtime.run_id) == records_before
        assert not llm.requests and not tools.inner.invocations


async def test_editing_or_removing_a_published_draft_fails_without_overwriting_it(tmp_path):
    async with selection(tmp_path) as (session, llm, tools):
        identity, runtime, original = await owned_original(session)
        context = LocalToolContext(session.deps, runtime)
        payload = draft(target=TARGET, evidence_id=str(original.evidence_id))
        assert (await publish_result_handler(publication([payload]), context)).ok
        saved = await session.deps.central_state.get_task(identity)
        changed = {**payload, "statement": "A different assertion with the same draft id"}
        with pytest.raises(StateConflict, match="original_result_assessment_changed"):
            await publish_result_handler(publication([changed]), context)
        stripped = InvestigationResult.model_validate({
            **saved.result.model_dump(mode="json"), "assessment_drafts": [],
        })
        with pytest.raises(StateConflict, match="original_result_assessment_changed"):
            await session.deps.central_state.transaction(lambda tx: tx.publish_result(
                identity, stripped, owner=runtime.work_owner,
                version=runtime.task_ownership_version, expected_hash=content_hash(saved),
            ))
        assert (await session.deps.central_state.get_task(identity)).result == saved.result


async def test_valid_span_and_diagnostic_survive_yield_without_candidate_verdict(tmp_path):
    async with selection(tmp_path) as (session, llm, tools):
        identity, runtime, original = await owned_original(session, candidate=True)
        graph = AttackGraphService(session.deps.graph_store)
        candidate_before = await graph.get_hypothesis("candidate")
        diagnostic = EvidenceRecord.model_validate({
            **original.model_dump(), "evidence_id": "ev-diagnostic", "source_type": "system",
            "source_id": "orchestration.investigation_result",
            "metadata": {"record_kind": "investigation_unresolved",
                         "mission_id": str(runtime.mission_id), "target_node_id": TARGET},
        })
        await session.deps.evidence_store.append(diagnostic)
        payload = draft(target=TARGET, evidence_id=str(original.evidence_id),
                        hypothesis_id="candidate", judgment="supports", severity="high",
                        limitation_refs=[str(diagnostic.evidence_id)])
        pointer = payload["observations"][0]["sources"][0]
        pointer.update(raw_path=["body_preview"], text_span={"offset": 9, "length": 4})
        payload["expectation"] = {
            "statement": "The recorded field supplies context, not a certified boundary.",
            "source": "evidence", "evidence_refs": [pointer],
        }
        assert (await publish_result_handler(
            publication([payload]), LocalToolContext(session.deps, runtime),
        )).ok
        published = (await session.deps.central_state.get_task(identity)).result
        await yield_probe(session.deps, {"runtime": runtime}, "Provider unavailable")
        saved = await session.deps.central_state.get_task(identity)
        assert saved.result.result_id == published.result_id
        assert saved.result.assessment_drafts == published.assessment_drafts
        assert saved.result.outcome == "inconclusive"
        assert await graph.get_hypothesis("candidate") == candidate_before
        assert await graph.list_findings() == []
        assert await session.deps.central_state.candidate_finding_promotions() == []
        assert not llm.requests and not tools.inner.invocations


class RetainedDraftModel(ReviewedObservationModel):
    """Script the storage flow; reuse the real original/Task completion checks."""

    def __init__(self, reason="The observation purpose is complete", outcome="success"):
        super().__init__()
        self.sent_draft = None
        self.settlement_reason = reason
        self.settlement_outcome = outcome

    async def complete_native(self, messages, tools, system=None, **kwargs):
        payload = worker_input(messages)
        if payload["probe"]["usage"]["actions"]:
            evidence = payload["probe"]["original_evidence_ids"]
            self.sent_draft = draft(target=TARGET, evidence_id=evidence[0], judgment="supports")
            # This parent only scripts an observation-purpose completion review.
            await super().complete_native(messages, tools, system=system, **kwargs)
            return turn(call("settle-draft", "task.settle", {
                "outcome": self.settlement_outcome, "reason": self.settlement_reason,
                "evidence_ids": evidence, "assessment_drafts": [self.sent_draft],
                "observations": ["The original status was retained."],
                "limitations": ["No candidate verdict is requested."],
            }))
        return await FakeLLMClient.complete_native(self, messages, tools, system=system, **kwargs)


@pytest.mark.parametrize("reason", ["The observation purpose is complete", ""])
@pytest.mark.parametrize("outcome", ["success", "refuted"])
async def test_native_reviewed_settlement_retains_draft(tmp_path, reason, outcome):
    llm = RetainedDraftModel(reason=reason, outcome=outcome)
    async with selection(tmp_path, llm=llm) as (session, llm, tools):
        identity = await register(session, "native-assessment", actions=2)
        llm.native_responses_by_task[identity] = [discover(TOOL), turn(inspect("observe"))]
        event, = await drive(session)
        assert event.kind == "completed", event.error
        saved = await session.deps.central_state.get_task(identity)
        assert saved.result.outcome == ("confirmed" if outcome == "success" else "refuted")
        assert saved.result.completion_receipt_id
        assert saved.result.assessment_drafts == [AssessmentDraft.model_validate(llm.sent_draft)]
        assert saved.result.observations == ["The original status was retained."]
        assert len(tools.inner.invocations) == 1
        assert await AttackGraphService(session.deps.graph_store).list_findings() == []
