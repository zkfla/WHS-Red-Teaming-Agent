"""Version-bound verdict admission with explicit provider/review doubles, no network."""

import pytest

from redteam.attack_graph.service import AttackGraphService
from redteam.core.enums import Verdict
from redteam.orchestration.analysis import VerificationAssessment, append_posthoc_claim_receipt
from redteam.orchestration.coordination import StateConflict, content_hash
from redteam.orchestration.specialist_tasks import HypothesisProbeProposal, register_probe_tasks
from tests.integration.test_central_completion import selection
from tests.integration.test_central_development import TARGET
from tests.integration.test_central_recovery import start


@pytest.mark.parametrize("outcome", ["success", "refuted"])
async def test_bound_final_verdict_and_stale_purpose_receipt(tmp_path, outcome):
    async with selection(tmp_path) as (session, llm):
        # Production-shaped original provenance from the existing protocol double.
        await start(session, llm)
        deps, state = session.deps, session.deps.central_state
        service = AttackGraphService(deps.graph_store)
        records = await deps.evidence_store.list_for_run(state.run_id)
        sources = [record for record in records if isinstance(record.metadata.get("tool_result"),
                                                              dict)]
        source = sources[0]
        mission = await state.get_mission(session.runtime.mission_id)
        binding = await state.transaction(lambda tx: tx.binding()[1], write=False)

        async def commit(identity, update):
            graph_read = {TARGET: content_hash(await service.get_node(TARGET))}
            if update.get("expected_revision") is not None:
                graph_read["hypothesis:candidate-a"] = content_hash(
                    await service.get_node("hypothesis:candidate-a"))
            return await register_probe_tasks(
                deps, proposal_id=identity,
                proposal=HypothesisProbeProposal.model_validate({"updates": [update]}),
                expected_revision=mission.revision, expected_binding_hash=binding,
                graph_read=graph_read, known_targets={TARGET}, known_principals=set(), max_tasks=1,
            )

        update = {"hypothesis": {
            "contract_version": 2, "hypothesis_id": "candidate-a", "description": "Access claim",
            "target_node_ids": [TARGET], "reason": "Inspect the original response",
        }}
        await commit("open", update)
        candidate = await service.get_hypothesis("candidate-a")
        assessment = VerificationAssessment(
            verdict=Verdict.PASS, reviewer_id="synthetic-independent-review",
            reason="Synthetic meaning judgment", source_refs=[str(source.evidence_id)],
            outcome=outcome, hypothesis_outcome=outcome, claim_supported=outcome == "success",
        )
        receipt = await append_posthoc_claim_receipt(
            evidence_store=deps.evidence_store, run_id=state.run_id,
            engagement_id=state.engagement_id, claim={"statement": candidate.description},
            evidence_ids=[str(source.evidence_id)], assessment=assessment,
            hypothesis_id=candidate.hypothesis_id, action_id=str(source.action_id),
            invocation_id=str(source.invocation_id), target_node_id=TARGET, candidate=candidate,
        )
        assert receipt is not None and receipt.metadata["candidate_revision"] == 1
        values = candidate.model_dump()
        values["status"] = "confirmed" if outcome == "success" else "rejected"
        evidence_field = ("supporting_evidence_ids" if outcome == "success"
                          else "refuting_evidence_ids")
        values[evidence_field] = [str(source.evidence_id)]
        change = {"hypothesis": values, "expected_revision": 1,
                  "review_evidence_id": str(receipt.evidence_id)}
        wrong_purpose = {**values, "premise": "A different untested prerequisite"}
        with pytest.raises(StateConflict, match="verdict_receipt_unbound"):
            await commit("wrong-purpose", {**change, "hypothesis": wrong_purpose})
        accepted = await commit("verdict", change)
        assert accepted["candidate_revisions"] == {"candidate-a": 2}
        saved = await service.get_hypothesis("candidate-a")
        assert saved.status == values["status"] and saved.revision == 2
        assert await service.list_findings() == []
        promotions = await state.candidate_finding_promotions()
        if outcome == "success":
            assert len(promotions) == 1
            assert promotions[0]["status"] == "refused"
            assert promotions[0]["reason"] == "review_did_not_confirm_security_violation"
        else:
            assert promotions == []
        await commit("reopen", {"hypothesis": {**saved.model_dump(), "status": "open"},
                                "expected_revision": 2})
        with pytest.raises(StateConflict, match="verdict_receipt_unbound"):
            await commit("old-review", {**change, "expected_revision": 3})
