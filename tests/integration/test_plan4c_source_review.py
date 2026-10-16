"""Stored evidence/reference contracts with explicit offline meaning doubles only."""

import json

import pytest

from redteam.attack_graph.service import AttackGraphService
from redteam.core.contracts.assessment import AssessmentDraft
from redteam.core.contracts.evidence import EvidenceRecord
from redteam.core.contracts.mission import InvestigationResult
from redteam.core.enums import TaskStatus, Verdict
from redteam.llm.fake import FakeLLMClient
from redteam.orchestration.analysis import (
    StructuredLLMAnalysisVerifier,
    VerificationAssessment,
    append_posthoc_claim_receipt,
)
from redteam.orchestration.assessment_drafts import validate_assessments
from redteam.orchestration.cognition import DecisionBudget
from redteam.orchestration.coordination import StateConflict
from redteam.orchestration.evidence_access import EvidenceReader
from redteam.orchestration.semantic_completion import prove_original
from tests.fakes.assessment_drafts import draft
from tests.fakes.worker_input import worker_input
from tests.integration.test_assessment_result_storage import owned_original
from tests.integration.test_central_development import TARGET, TOOL, call, discover, inspect, turn
from tests.integration.test_probe_workers import (
    ReviewedObservationModel,
    drive,
    register,
    selection,
)


def source(evidence_id, path, role="support", **changes):
    return {"evidence_id": evidence_id, "raw_path": path, "role": role, **changes}


def assessment(evidence_id, sources):
    return AssessmentDraft.model_validate(draft(
        target=TARGET, evidence_id=evidence_id,
        observations=[{"statement": "Inspect the retained fields.", "sources": sources}],
    ))


def meaning(payload, outcome):
    return {
        "meaning": "Scripted interpretation of the displayed fixture fields.",
        "reason": "Offline contract test, not evidence of detection performance.",
        "outcome": outcome, "claim_supported": outcome == "success", "confidence": 0.8,
        "adopted_detail": {
            "title": payload.name, "statement": payload.statement,
            "summary": payload.reason, "vulnclass": payload.vulnclass,
            "severity": payload.severity, "limitations": list(payload.limitations),
        },
    }


async def review(session, original, payload, outcome="success", refs=None, binding=None):
    llm = FakeLLMClient(canned_structured=meaning(payload, outcome))
    verifier = StructuredLLMAnalysisVerifier(
        llm, session.deps.evidence_store, AttackGraphService(session.deps.graph_store),
    )
    result = await verifier.verify_claim_with_budget(
        claim={"statement": payload.statement, "target_node_id": TARGET,
               "principal_refs": payload.principal_refs,
               "assessment_draft": payload.model_dump(mode="json"),
               "assessment_draft_binding": binding or payload.identity("result-fixture-review")},
        evidence_ids=refs if refs is not None else [str(original.evidence_id)],
        run_id=str(session.runtime.run_id), budget=DecisionBudget(3), security_claim=True,
    )
    return result, llm


@pytest.mark.parametrize("path", [[], ["output"], ["status"], ["output", "status"]])
async def test_storage_and_review_accept_the_same_structured_or_scalar_reference(tmp_path, path):
    async with selection(tmp_path) as (session, _, _):
        identity, runtime, original = await owned_original(session)
        task = await session.deps.central_state.get_task(identity)
        payload = assessment(str(original.evidence_id), [source(str(original.evidence_id), path)])
        stored = InvestigationResult(
            contract_version=2, result_id="result-shared-fields", outcome="inconclusive",
            summary="Store the selected reference", assessment_drafts=[payload],
        )
        await validate_assessments(session.deps, runtime, task, stored)
        result, llm = await review(session, original, payload)
        assert result.outcome == "success" and len(llm.requests) == 1
        window = result.source_review["sources"][0]["windows"][0]
        assert window["status"] == "ok" and window["raw_path"] == path
        assert window["required"] and window["role"] == "support"
        if len(path) <= 1 and path != ["status"]:
            assert window["output_fields"]["shown_field_count"] == 2
        assert "proposal" not in window and "provenance" not in window
        assert "approval" not in json.dumps(result.source_review)


@pytest.mark.parametrize("role,expected", [("context", "success"), ("support", "unknown"),
                                          ("refute", "unknown")])
async def test_optional_missing_window_differs_from_missing_essential_window(
    tmp_path, role, expected,
):
    async with selection(tmp_path) as (session, _, _):
        _, _, original = await owned_original(session)
        evidence_id = str(original.evidence_id)
        payload = assessment(evidence_id, [source(evidence_id, ["status"]),
                                           source(evidence_id, ["missing"], role)])
        result, llm = await review(session, original, payload)
        assert result.outcome == expected and len(llm.requests) == (expected == "success")
        windows = result.source_review["sources"][0]["windows"]
        missing = next(window for window in windows if window["raw_path"] == ["missing"])
        assert missing["status"] == "field_unavailable" and missing["role"] == role
        receipt = await append_posthoc_claim_receipt(
            evidence_store=session.deps.evidence_store, run_id=str(session.runtime.run_id),
            engagement_id=str(session.runtime.engagement_id),
            claim={"statement": payload.statement}, evidence_ids=[evidence_id], assessment=result,
            action_id=str(original.action_id), invocation_id=str(original.invocation_id),
            target_node_id=TARGET,
        )
        assert receipt is not None and receipt.metadata["source_review"] == result.source_review


@pytest.mark.parametrize("outcome", ["success", "refuted", "unknown"])
async def test_text_span_is_the_actual_reviewed_window_and_verdict_is_independent(
    tmp_path, outcome,
):
    async with selection(tmp_path) as (session, _, _):
        _, _, original = await owned_original(session)
        payload = assessment(str(original.evidence_id), [source(
            str(original.evidence_id), ["body_preview"],
            text_span={"offset": 9, "length": 4},
        )])
        result, llm = await review(session, original, payload, outcome)
        assert result.outcome == outcome and len(llm.requests) == 1
        window = result.source_review["sources"][0]["windows"][0]
        assert window["text"] == "safe" and window["truncated"]
        assert window["displayed_range"] == {"start": 9, "end": 13}
        assert window["text_span"] == {"offset": 9, "length": 4}
        assert result.source_review["draft_binding"]["content_hash"] == (
            payload.identity("result-fixture-review")["content_hash"]
        )


@pytest.mark.parametrize("breach", ["missing", "redacted", "span", "required_omitted",
                                    "foreign_run", "target", "principal", "digest_only"])
async def test_unavailable_essential_sources_remain_unknown_without_meaning_calls(tmp_path, breach):
    async with selection(tmp_path) as (session, _, _):
        _, _, original = await owned_original(session)
        path, extra, principal = ["missing"], {}, []
        if breach == "redacted":
            path = ["password"]
        elif breach == "span":
            path, extra = ["body_preview"], {"text_span": {"offset": 500, "length": 1}}
        elif breach == "foreign_run":
            original = original.model_copy(update={
                "run_id": "foreign", "evidence_id": "ev-foreign",
            })
        elif breach == "target":
            original = original.model_copy(update={"evidence_id": "ev-target", "metadata": {
                **original.metadata, "target_node_id": "host:other.local",
            }})
        elif breach == "digest_only":
            original = original.model_copy(update={"evidence_id": "ev-digest", "metadata": {}})
        elif breach == "principal":
            path, principal = ["status"], ["principal-without-proof"]
        if await session.deps.evidence_store.get(original.evidence_id) is None:
            await session.deps.evidence_store.append(original)
        evidence_id = str(original.evidence_id)
        refs = [source(evidence_id, path, **extra)]
        if breach == "required_omitted":
            # Distinct selectors exceed the per-source window bound.
            refs = [source(evidence_id, ["body_preview"], text_span={"offset": i, "length": 1})
                    for i in range(5)]
        payload = assessment(evidence_id, refs).model_copy(update={"principal_refs": principal})
        result, llm = await review(session, original, payload)
        assert result.verdict is Verdict.UNKNOWN and result.outcome == "unknown"
        assert not llm.requests


async def test_review_and_diagnostic_records_cannot_be_execution_originals(tmp_path):
    async with selection(tmp_path) as (session, _, _):
        _, _, original = await owned_original(session)
        system = EvidenceRecord.model_validate({
            **original.model_dump(), "evidence_id": "ev-review", "source_type": "system",
            "metadata": {"record_kind": "review_receipt", "public": True, "admission": "final"},
        })
        await session.deps.evidence_store.append(system)
        reader = EvidenceReader(session.deps.evidence_store)
        displayed = await reader.read(str(system.evidence_id), str(session.runtime.run_id),
                                      engagement_id=str(session.runtime.engagement_id))
        assert displayed["view_kind"] == "review"
        record, proof = await prove_original(
            evidence_store=session.deps.evidence_store, path_store=None,
            run_id=str(session.runtime.run_id), engagement_id=str(session.runtime.engagement_id),
            mission_id=str(session.runtime.mission_id), evidence_id=str(system.evidence_id),
        )
        assert record is None and proof.issues[0].code == "origin_record_not_execution"


async def test_missing_path_is_also_rejected_before_storage(tmp_path):
    async with selection(tmp_path) as (session, _, _):
        identity, runtime, original = await owned_original(session)
        task = await session.deps.central_state.get_task(identity)
        payload = assessment(
            str(original.evidence_id), [source(str(original.evidence_id), ["missing"])],
        )
        result = InvestigationResult(contract_version=2, result_id="missing-field",
                                     outcome="inconclusive", summary="Invalid stored source",
                                     assessment_drafts=[payload])
        with pytest.raises(StateConflict, match="assessment_source_path_missing"):
            await validate_assessments(session.deps, runtime, task, result)


async def test_changed_draft_binding_is_unknown_before_reading_or_reviewing(tmp_path):
    async with selection(tmp_path) as (session, _, _):
        _, _, original = await owned_original(session)
        payload = assessment(str(original.evidence_id), [source(str(original.evidence_id), [])])
        binding = {**payload.identity("result-fixture-review"), "content_hash": "0" * 64}
        result, llm = await review(session, original, payload, binding=binding)
        assert result.outcome == "unknown" and result.source_review is None
        assert not llm.requests


class MixedEvidenceModel(ReviewedObservationModel):
    """Scripted observation settlement that cites both TOOL and SYSTEM records."""

    session = None
    auxiliary = None

    async def complete_native(self, messages, tools, system=None, **kwargs):
        payload = worker_input(messages)
        if not payload["probe"]["usage"]["actions"]:
            return await super().complete_native(messages, tools, system=system, **kwargs)
        await super().complete_native(messages, tools, system=system, **kwargs)
        originals = payload["probe"]["original_evidence_ids"]
        store = self.session.deps.evidence_store
        original = await store.get(originals[0])
        if self.auxiliary is None:
            self.auxiliary = await append_posthoc_claim_receipt(
                evidence_store=store, run_id=str(self.session.runtime.run_id),
                engagement_id=str(self.session.runtime.engagement_id),
                claim={"statement": "The fixture response was observed."},
                evidence_ids=originals, action_id=str(original.action_id),
                invocation_id=str(original.invocation_id), target_node_id=TARGET,
                assessment=VerificationAssessment(
                    verdict=Verdict.PASS, reviewer_id="fixture-independent-observation-review",
                    outcome="success", claim_supported=True, source_refs=originals,
                    reason="Scripted observation review, not a security verdict.",
                ),
            )
        assert self.auxiliary is not None
        return turn(call("settle-with-context", "task.settle", {
            "outcome": "success", "reason": "Complete only the observation purpose",
            "evidence_ids": [*originals, str(self.auxiliary.evidence_id)],
        }))


async def test_completion_does_not_prove_a_review_receipt_as_a_target_execution(tmp_path):
    llm = MixedEvidenceModel()
    async with selection(tmp_path, llm=llm) as (session, _, _):
        llm.session = session
        identity = await register(session, "mixed-evidence-observation", actions=2)
        llm.native_responses_by_task[identity] = [discover(TOOL), turn(inspect("original"))]
        outcome, = await drive(session)
        assert outcome.kind == "completed", outcome.error
        saved = await session.deps.central_state.get_task(identity)
        assert saved.status is TaskStatus.COMPLETED
        assert saved.result.outcome == "confirmed", (
            saved.result.summary,
            [json.loads(message["content"]).get("error")
             for message in llm.last_native_request["messages"] if message["role"] == "tool"],
        )
        receipt = await session.deps.evidence_store.get(saved.completion_evidence_id)
        assert llm.auxiliary.evidence_id not in receipt.metadata["source_evidence_ids"]
        assert receipt.metadata["source_evidence_ids"]
        assert await AttackGraphService(session.deps.graph_store).list_findings() == []
