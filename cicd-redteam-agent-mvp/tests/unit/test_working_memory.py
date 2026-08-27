from __future__ import annotations

import json
from pathlib import Path

import pytest

from cicd_redteam_agent.application.state import create_initial_state
from cicd_redteam_agent.domain.models import (
    Evidence,
    Relation,
    TriState,
    WorkingMemoryPatch,
)
from cicd_redteam_agent.framework.capabilities import CapabilityContext, build_capabilities
from cicd_redteam_agent.framework.working_memory import (
    apply_working_memory_patch,
    expand_working_memory_patch_refs,
    sanitize_working_memory_patch_json,
)
from cicd_redteam_agent.security.repository_boundary import RepositoryBoundary


def _state(tmp_path: Path):
    repository = tmp_path / "repository"
    repository.mkdir()
    state = create_initial_state(RepositoryBoundary(repository))
    state.evidence["ev_test"] = Evidence(
        evidence_id="ev_test",
        kind="test",
        source_path=".github/workflows/ci.yml",
        line_start=1,
        line_end=1,
        field_path="$.jobs.test",
        redacted_excerpt="test",
        content_hash="0" * 64,
    )
    state.relations = [
        Relation(
            relation_id="rel_test",
            source_id="source:test",
            relation_type="reaches",
            target_id="sink:test",
            category="execution",
            verdict=TriState.TRUE,
            evidence_ids=["ev_test"],
        )
    ]
    return state


def _create_patch() -> WorkingMemoryPatch:
    return WorkingMemoryPatch.model_validate(
        {
            "expected_revision": 0,
            "upserts": [
                {
                    "claim": "PR input may reach a privileged execution step",
                    "status": "ACTIVE",
                    "decision_summary": "A source relation exists; execution needs checking.",
                    "next_check": {
                        "objective": "Inspect the target workflow",
                        "preferred_capability": "GetWorkflowFacts",
                        "target": "wf_release",
                    },
                }
            ],
        }
    )


def test_working_memory_short_refs_restore_grounding(tmp_path: Path) -> None:
    state = _state(tmp_path)
    state.relations[0].verdict = TriState.UNKNOWN
    state.relations[0].missing_evidence = ["runtime authorization"]

    patch = expand_working_memory_patch_refs(
        state,
        json.dumps(
            {
                "schema_version": "1.0",
                "expected_revision": 0,
                "upserts": [
                    {
                        "claim": "A returned relation may reach the test sink",
                        "status": "ACTIVE",
                        "supporting_relation_refs": ["R0001"],
                        "decision_summary": "Inspect the bounded sink context next.",
                    }
                ],
            }
        ),
    )

    upsert = patch.upserts[0]
    assert upsert.supporting_relation_ids == ["rel_test"]
    assert upsert.evidence_ids == ["ev_test"]
    assert upsert.missing_evidence == ["runtime authorization"]
    assert apply_working_memory_patch(
        state, patch, allowed_capabilities={"GetWorkflowFacts"}
    ).accepted


def test_working_memory_short_refs_reject_unknown_ref(tmp_path: Path) -> None:
    state = _state(tmp_path)

    with pytest.raises(ValueError, match="unknown relation refs"):
        expand_working_memory_patch_refs(
            state,
            json.dumps(
                {
                    "expected_revision": 0,
                    "upserts": [
                        {
                            "claim": "Unknown relation",
                            "supporting_relation_refs": ["R9999"],
                            "decision_summary": "This must not mutate state.",
                        }
                    ],
                }
            ),
        )
    assert state.working_memory.revision == 0


def test_working_memory_create_update_and_semantic_noop(tmp_path: Path) -> None:
    state = _state(tmp_path)
    allowed = {"GetWorkflowFacts", "SubmitAnalysis"}

    created = apply_working_memory_patch(
        state,
        _create_patch(),
        allowed_capabilities=allowed,
    )

    assert created.accepted and created.changed
    assert created.revision == 1
    hypothesis_id = created.updated_hypothesis_ids[0]
    assert state.working_memory.focus_hypothesis_id == hypothesis_id

    ready_patch = WorkingMemoryPatch.model_validate(
        {
            "expected_revision": 1,
            "upserts": [
                {
                    "hypothesis_id": hypothesis_id,
                    "status": "READY_FOR_PROPOSAL",
                    "supporting_relation_ids": ["rel_test", "rel_test"],
                    "evidence_ids": ["ev_test", "ev_test"],
                    "decision_summary": "The extracted relation and Evidence support review.",
                    "clear_next_check": True,
                }
            ],
        }
    )
    ready = apply_working_memory_patch(state, ready_patch, allowed_capabilities=allowed)

    assert ready.accepted and ready.changed
    assert state.working_memory.revision == 2
    hypothesis = state.working_memory.hypotheses[0]
    assert hypothesis.supporting_relation_ids == ["rel_test"]
    assert hypothesis.evidence_ids == ["ev_test"]

    noop_patch = WorkingMemoryPatch.model_validate(
        {
            "expected_revision": 2,
            "upserts": [
                {
                    "hypothesis_id": hypothesis_id,
                    "status": "READY_FOR_PROPOSAL",
                    "supporting_relation_ids": ["rel_test"],
                    "evidence_ids": ["ev_test"],
                    "decision_summary": "The extracted relation and Evidence support review.",
                    "clear_next_check": True,
                }
            ],
        }
    )
    noop = apply_working_memory_patch(state, noop_patch, allowed_capabilities=allowed)

    assert noop.accepted and not noop.changed
    assert state.working_memory.revision == 2


def test_working_memory_rejects_unknown_grounding_without_mutation(tmp_path: Path) -> None:
    state = _state(tmp_path)
    result = apply_working_memory_patch(
        state,
        WorkingMemoryPatch.model_validate(
            {
                "expected_revision": 0,
                "upserts": [
                    {
                        "claim": "Unknown relation reaches a sink",
                        "status": "READY_FOR_PROPOSAL",
                        "supporting_relation_ids": ["rel_missing"],
                        "evidence_ids": ["ev_missing"],
                        "decision_summary": "This must be rejected.",
                    }
                ],
            }
        ),
        allowed_capabilities={"SubmitAnalysis"},
    )

    assert not result.accepted
    assert state.working_memory.revision == 0
    assert state.working_memory.hypotheses == []


def test_working_memory_rejects_stale_revision(tmp_path: Path) -> None:
    state = _state(tmp_path)
    stale_patch = _create_patch().model_copy(update={"expected_revision": 1})

    result = apply_working_memory_patch(
        state,
        stale_patch,
        allowed_capabilities={"GetWorkflowFacts"},
    )

    assert not result.accepted
    assert result.errors == [
        "working_memory_revision_conflict: expected 1, current 0"
    ]
    assert state.working_memory.revision == 0


@pytest.mark.parametrize(
    ("verdict", "relation_missing_evidence", "expected_error"),
    [
        (
            TriState.FALSE,
            [],
            "FALSE relations cannot support a hypothesis",
        ),
        (
            TriState.UNKNOWN,
            ["runtime trust configuration"],
            "hypothesis omits UNKNOWN relation missing evidence",
        ),
    ],
)
def test_working_memory_preserves_relation_verdict_constraints(
    tmp_path: Path,
    verdict: TriState,
    relation_missing_evidence: list[str],
    expected_error: str,
) -> None:
    state = _state(tmp_path)
    state.relations[0] = state.relations[0].model_copy(
        update={
            "verdict": verdict,
            "missing_evidence": relation_missing_evidence,
        }
    )
    patch = WorkingMemoryPatch.model_validate(
        {
            "expected_revision": 0,
            "upserts": [
                {
                    "claim": "The extracted relation supports a privileged path",
                    "status": "READY_FOR_PROPOSAL",
                    "supporting_relation_ids": ["rel_test"],
                    "evidence_ids": ["ev_test"],
                    "decision_summary": "Validate deterministic relation semantics.",
                }
            ],
        }
    )

    result = apply_working_memory_patch(
        state,
        patch,
        allowed_capabilities={"SubmitAnalysis"},
    )

    assert not result.accepted
    assert any(expected_error in error for error in result.errors)
    assert state.working_memory.revision == 0


@pytest.mark.asyncio
async def test_memory_patch_piggybacks_without_changing_action_signature(
    tmp_path: Path,
) -> None:
    state = _state(tmp_path)
    boundary = RepositoryBoundary(tmp_path / "repository")
    context = CapabilityContext(state, boundary)
    capability = build_capabilities(context)["ListWorkflows"]
    schema = capability.to_model()
    assert "working_memory_patch_json" in schema.model_fields

    first = schema.model_validate(
        {"working_memory_patch_json": _create_patch().model_dump_json()}
    )
    first_result = json.loads(await first.execute())
    hypothesis_id = first_result["working_memory_update"]["updated_hypothesis_ids"][0]
    assert first_result["capability_result"] == {"workflows": []}

    second_patch = WorkingMemoryPatch.model_validate(
        {
            "expected_revision": 1,
            "upserts": [
                {
                    "hypothesis_id": hypothesis_id,
                    "status": "BLOCKED",
                    "missing_evidence": ["workflow facts"],
                    "decision_summary": "No parsed workflow is available.",
                    "clear_next_check": True,
                }
            ],
        }
    )
    second = schema.model_validate(
        {"working_memory_patch_json": second_patch.model_dump_json()}
    )
    await second.execute()

    assert state.working_memory.revision == 2
    assert len(state.repeated_calls) == 1
    assert max(state.repeated_calls.values()) == 2


@pytest.mark.asyncio
async def test_follow_up_investigation_requires_initial_hypothesis_patch(
    tmp_path: Path,
) -> None:
    state = _state(tmp_path)
    boundary = RepositoryBoundary(tmp_path / "repository")
    context = CapabilityContext(state, boundary)
    capabilities = build_capabilities(context)

    list_facts = capabilities["ListSecurityFacts"].to_model().model_validate({})
    await list_facts.execute()

    follow_up_schema = capabilities["GetWorkflowFacts"].to_model()
    rejected = json.loads(
        await follow_up_schema.model_validate({"workflow_id": "wf_missing"}).execute()
    )
    assert rejected["error"] == (
        "working_memory_patch_required_for_follow_up_investigation"
    )

    accepted = json.loads(
        await follow_up_schema.model_validate(
            {
                "workflow_id": "wf_missing",
                "working_memory_patch_json": _create_patch().model_dump_json(),
            }
        ).execute()
    )
    assert accepted["working_memory_update"]["accepted"] is True
    assert accepted["capability_result"]["error"] == "unknown_workflow_id"
    assert state.working_memory.revision == 1


def test_patch_redaction_preserves_grounding_ids() -> None:
    raw = json.dumps(
        {
            "expected_revision": 1,
            "upserts": [
                {
                    "hypothesis_id": "hyp_0123456789abcdef",
                    "evidence_ids": ["ev_AKIAEXACTIDENTIFIER"],
                    "supporting_relation_ids": ["rel_secret_access"],
                    "decision_summary": "token=super-secret-value",
                }
            ],
        }
    )

    sanitized = json.loads(sanitize_working_memory_patch_json(raw))

    assert sanitized["upserts"][0]["evidence_ids"] == ["ev_AKIAEXACTIDENTIFIER"]
    assert sanitized["upserts"][0]["supporting_relation_ids"] == ["rel_secret_access"]
    assert "super-secret-value" not in sanitized["upserts"][0]["decision_summary"]
