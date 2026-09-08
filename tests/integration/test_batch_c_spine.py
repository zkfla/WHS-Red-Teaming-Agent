from __future__ import annotations

import json
from pathlib import Path

from redteam_core.acquisition import (
    AcquisitionBudget,
    ArtifactBoundary,
    search_artifact_text,
)
from redteam_core.knowledge import CoreSession
from redteam_core.models import Relation, TriState, WorkingHypothesisUpsert, WorkingMemoryPatch
from redteam_core.reasoning import apply_working_memory_patch, project_decision_context


def test_batch_c_bounded_artifact_to_grounded_memory_to_context_spine(tmp_path: Path):
    root = tmp_path / "artifacts"
    root.mkdir()
    (root / "source.txt").write_text("untrusted marker reaches operation\n", encoding="utf-8")
    boundary = ArtifactBoundary(root)
    search = search_artifact_text(boundary, AcquisitionBudget(), "marker")
    assert len(search.hits) == 1

    session = CoreSession()
    relation = Relation.create(
        source_ref="artifact:source.txt",
        relation_type_id="batchc.relation.observed@1",
        target_ref="semantic:operation",
        verdict=TriState.TRUE,
        evidence_ids=["ev_batchc0000000001"],
    )
    session.semantic.relations[relation.relation_id] = relation
    patch = WorkingMemoryPatch(
        expected_revision=0,
        upserts=(
            WorkingHypothesisUpsert(
                semantic_key="batchc:hypothesis:one",
                claim="Investigate the observed semantic relation",
                supporting_relation_ids=(relation.relation_id,),
                decision_summary="Grounded from bounded artifact acquisition.",
            ),
        ),
    )
    update = apply_working_memory_patch(session, patch)
    assert update.accepted and update.changed
    hypothesis = next(iter(session.working_memory.hypotheses.values()))
    assert hypothesis.evidence_ids == ("ev_batchc0000000001",)

    projection = project_decision_context(session)
    payload = json.loads(projection.content)
    working = payload["channels"]["LLM_WORKING_TEXT"]
    canonical = payload["channels"]["CANONICAL_STATE"]
    assert working["hypotheses"][0]["supporting_relation_ids"] == [relation.relation_id]
    assert canonical["relations"][0]["relation_id"] == relation.relation_id
