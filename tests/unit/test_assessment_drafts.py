"""Draft normalization, immutable identity and legacy result/hash compatibility."""

from copy import deepcopy

import pytest
from pydantic import ValidationError

from redteam.core.contracts.assessment import AssessmentDraft, FindingDraft
from redteam.core.contracts.completion import completion_contract_hash
from redteam.core.contracts.mission import InvestigationResult
from redteam.core.evidence_paths import walk_output
from redteam.orchestration.central_results import result_for
from redteam.orchestration.checkpointing import build_checkpointer, state_type_allowlist
from redteam.orchestration.completion_proposal import CompletionProposal
from redteam.orchestration.coordination import StateConflict, content_hash
from redteam.orchestration.graph_recording import FindingDraft as RecordingDraft
from tests.fakes.assessment_drafts import draft
from tests.unit.test_central_state import task


def result(**changes):
    return InvestigationResult.model_validate({
        "result_id": "result-a", "outcome": "inconclusive", "summary": "Retained data",
        "contract_version": 2, **changes,
    })


def test_draft_masks_narratives_without_carrying_a_free_form_excerpt():
    payload = draft(
        statement="Stored proposal password=DraftSentinel",
        impact="Potential scope token=ImpactSentinel",
        limitations=["Unproven secret=LimitSentinel"],
        alternative_explanations=["Assumption api_key=AlternativeSentinel"],
    )
    stored = AssessmentDraft.model_validate(payload)
    text = stored.model_dump_json()
    assert all(marker not in text for marker in [
        "DraftSentinel", "ImpactSentinel", "LimitSentinel", "AlternativeSentinel",
    ])
    assert "REDACTED" in text
    with pytest.raises(ValidationError) as failure:
        AssessmentDraft.model_validate({**payload, "run_id": "ModelCannotAssignThis"})
    assert "ModelCannotAssignThis" not in str(failure.value)


@pytest.mark.parametrize("path", [[True], [-1], ["-1"], [{}]])
def test_draft_rejects_invalid_path_segments(path):
    payload = draft(evidence_id="ev-a")
    payload["observations"][0]["sources"][0]["raw_path"] = path
    with pytest.raises(ValidationError):
        AssessmentDraft.model_validate(payload)


def test_path_resolution_keeps_existing_output_relative_and_envelope_semantics():
    assert walk_output({"rows": [{"label": "retained"}]}, ["rows", "0", "label"]) == (
        True, "retained",
    )
    assert walk_output({"rows": [7]}, ["output", "rows", 0]) == (True, 7)
    assert walk_output({"output": {"label": 8}}, ["output", "label"]) == (True, 8)
    assert walk_output({"rows": [7]}, ["rows", 2]) == (False, None)


def test_long_claim_is_retained_and_hashed_completely_but_not_silently_shortened_for_review():
    statement = "a" * 600 + " This observation does not establish a security violation."
    stored = AssessmentDraft.model_validate(draft(statement=statement))
    identity = stored.identity("result-a")
    assert identity == AssessmentDraft.model_validate_json(stored.model_dump_json()).identity(
        "result-a",
    )
    changed = stored.model_copy(update={"statement": "a" * 600 + " Other scope."})
    assert changed.identity("result-a")["content_hash"] != identity["content_hash"]
    assert stored.statement == statement
    with pytest.raises(ValueError, match="assessment_claim_requires_full_length_review"):
        stored.as_finding_draft()
    assert issubclass(RecordingDraft, FindingDraft)
    assert RecordingDraft.model_json_schema() == FindingDraft.model_json_schema()
    short = AssessmentDraft.model_validate(draft(evidence_id="ev-a"))
    assert short.as_finding_draft().statement == short.statement
    assert short.as_finding_draft().evidence_refs == ["ev-a"]
    with pytest.raises(ValidationError):
        AssessmentDraft.model_validate(draft(statement="a" * 4001))


def test_legacy_result_and_completion_wire_and_purpose_hash_are_unchanged_when_empty():
    for version in (1, 2):
        old = {"result_id": "result-a", "outcome": "inconclusive", "summary": "Retained data",
               "evidence_ids": [], "hypothesis_ids": [], "finding_node_ids": [],
               "completion_receipt_id": None, "questions": []}
        if version == 2:
            old.update(contract_version=2, observations=[], limitations=[])
        loaded = InvestigationResult.model_validate(old)
        assert loaded.model_dump(mode="json") == old
        assert content_hash(loaded) == content_hash(old)
        assert InvestigationResult.model_validate_json(loaded.model_dump_json()) == loaded
    proposal = CompletionProposal(outcome="inconclusive", reason="Retained data")
    assert set(proposal.model_dump()) == {
        "outcome", "reason", "evidence_ids", "criterion_results", "gaps",
    }
    base = task()
    with_result = base.model_copy(update={"result": result(assessment_drafts=[draft()])})
    assert completion_contract_hash(base) == completion_contract_hash(with_result)
    with pytest.raises(ValidationError, match="result contract v2"):
        result(contract_version=1, assessment_drafts=[draft()])


def test_same_id_replay_is_stable_and_edits_or_duplicate_ids_are_rejected():
    stored = result(assessment_drafts=[draft()])
    base = task().model_copy(update={"investigation_contract_version": 2, "result": stored})
    merged = result_for(base, outcome="inconclusive", summary=stored.summary, evidence_ids=[],
                        assessment_drafts=stored.assessment_drafts)
    assert merged == stored
    incoming = AssessmentDraft.model_validate(draft(statement="Different assertion"))
    with pytest.raises(StateConflict, match="original_result_assessment_changed"):
        result_for(base, outcome="inconclusive", summary="Retain", evidence_ids=[],
                   assessment_drafts=[incoming])
    payload = deepcopy(draft())
    with pytest.raises(ValidationError, match="draft ids must be unique"):
        result(assessment_drafts=[payload, payload])


def test_sdk_checkpoint_preserves_new_drafts_and_legacy_recording_type():
    saver = build_checkpointer(enabled=True)
    assert saver is not None
    legacy = RecordingDraft(statement="Historical claim")
    assert ("redteam.orchestration.graph_recording", "FindingDraft") in state_type_allowlist()
    stored = {"result": result(assessment_drafts=[draft()]), "legacy": legacy}
    restored = saver.serde.loads_typed(saver.serde.dumps_typed(stored))
    assert isinstance(restored["result"], InvestigationResult)
    assert isinstance(restored["result"].assessment_drafts[0], AssessmentDraft)
    assert isinstance(restored["legacy"], RecordingDraft)
    assert restored == stored


def test_model_copy_input_is_normalized_again_before_result_storage():
    incoming = AssessmentDraft.model_validate(draft()).model_copy(update={
        "statement": "Proposed assertion password=CopySentinel",
    })
    base = task().model_copy(update={"investigation_contract_version": 2})
    stored = result_for(base, outcome="inconclusive", summary="Retain", evidence_ids=[],
                        assessment_drafts=[incoming])
    assert "CopySentinel" not in stored.model_dump_json()
    assert "REDACTED" in stored.assessment_drafts[0].statement
