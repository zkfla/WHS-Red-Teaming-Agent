"""Real completion gate, synthetic original records and a scripted reviewer."""

import json

import pytest
from pydantic import ValidationError

from redteam.attack_graph.observation import GraphObservation
from redteam.attack_graph.service import AttackGraphService
from redteam.core.contracts.completion import (
    CompletionCriterion,
    completion_conditions,
    completion_contract_hash,
)
from redteam.core.contracts.mission import MissionTask
from redteam.core.enums import TaskStatus
from redteam.core.ids import EvidenceId
from redteam.orchestration.coordination import StateConflict
from redteam.orchestration.engine import InvestigationEngine, TurnDecision
from redteam.orchestration.semantic_completion import verify_task_receipt
from redteam.orchestration.task_graph.sqlite import SQLiteTaskGraphStore
from tests.integration import _engine_llm_harness as h
from tests.integration.test_completion_review_atomicity import Reviewer, setup


class ContractReviewer(Reviewer):
    def __init__(self):
        super().__init__()
        self.payloads = []
        self.omit = False
        self.claim_all = True

    async def complete_structured(self, **kwargs):
        self.calls += 1
        if self.hook:
            await self.hook()
        payload = json.loads(kwargs["messages"][0]["content"])
        self.payloads.append(payload)
        identities = payload["verified_source_ids"]
        results = [{
            "criterion_id": c["criterion_id"], "met": self.claim_all,
            "evidence_ids": identities, "reason": "both original records compared",
        } for c in payload["conditions"]]
        if self.omit:
            results[0]["criterion_id"] = "invented-condition"
        return {**h.task_accept(identities), "criterion_results": results}


async def contracted_world(*, origins="two", reviewer=None):
    world, runtime, path, reviewer = await setup("task", reviewer or ContractReviewer())
    task = await world.tasks.get_task(runtime.active_task_id)
    task = task.model_copy(update={
        "completion_contract_version": 2,
        "principal_refs": ["account-a", "account-b"],
        "completion_criteria": [CompletionCriterion(
            kind="semantic", description="Compare original responses for both principals",
            min_evidence=2, principal_refs=["account-a", "account-b"],
        )],
    })
    await world.tasks.update_task(task)
    base = await world.evidence.get("proof-race")
    evidence_ids = []
    count = 1 if origins == "one" else 2
    for index in range(count):
        record = base.model_copy(deep=True, update={"evidence_id": EvidenceId(f"origin-{index}")})
        principal = "account-a" if index == 0 or origins == "same-principal" else "account-b"
        action = f"action-{index}"
        invocation = f"inv-{index}"
        if origins == "alias":
            action, invocation = "action-0", "inv-0"
        record.action_id = action
        record.invocation_id = invocation
        meta = record.metadata
        meta.update(action_id=action, invocation_id=invocation, principal_ref=principal)
        meta["proposal"].update(action_id=action, arguments={"session_ref": principal})
        meta["tool_result"].update(action_id=action, invocation_id=invocation, output={
            "status": 403, "principal_ref": principal, "actual_session_ref": principal,
        })
        if origins == "failed" and index == 1:
            meta["success"] = False
            meta["tool_result"]["success"] = False
        await world.evidence.append(record)
        evidence_ids.append(str(record.evidence_id))
    return world, runtime, path, reviewer, evidence_ids, task


async def complete(world, runtime, path, identities):
    engine = InvestigationEngine(world.deps)
    context = await engine.context(runtime, GraphObservation())
    assert context["active_task"]["completion_contract_version"] == 2
    return await engine.complete_task(
        path,
        TurnDecision(mode="complete_task", reason="compare", completion={
            "outcome": "success", "reason": "compare", "evidence_ids": identities,
        }), runtime, context,
    )


@pytest.mark.parametrize("origins", ["one", "same-principal", "alias", "failed"])
async def test_fixed_conditions_reject_before_semantic_call(origins):
    world, runtime, path, reviewer, identities, _ = await contracted_world(origins=origins)
    errors = await complete(world, runtime, path, identities)
    assert errors
    assert reviewer.calls == 0
    assert (await world.tasks.get_task(runtime.active_task_id)).status == TaskStatus.RUNNING


@pytest.mark.parametrize("origins", ["failed", "alias"])
@pytest.mark.parametrize("reverse", [False, True])
async def test_extra_original_does_not_hide_invalid_principal_coverage(origins, reverse):
    world, runtime, path, reviewer, identities, _ = await contracted_world(origins=origins)
    extra = (await world.evidence.get(identities[0])).model_copy(deep=True)
    extra.evidence_id = EvidenceId("extra-account-a")
    extra.action_id, extra.invocation_id = "extra-action", "extra-invocation"
    extra.metadata.update(action_id=extra.action_id, invocation_id=extra.invocation_id)
    extra.metadata["proposal"]["action_id"] = extra.action_id
    extra.metadata["tool_result"].update(
        action_id=extra.action_id, invocation_id=extra.invocation_id,
    )
    await world.evidence.append(extra)
    if reverse:
        identities.reverse()
    assert await complete(world, runtime, path, [*identities, str(extra.evidence_id)])
    assert reviewer.calls == 0


async def test_two_originals_complete_investigation_not_a_finding():
    world, runtime, path, reviewer, identities, task = await contracted_world()
    assert await complete(world, runtime, path, identities) == []
    stored = await world.tasks.get_task(runtime.active_task_id)
    assert stored.status == TaskStatus.COMPLETED
    receipt = await world.evidence.get(stored.completion_evidence_id)
    assert verify_task_receipt(stored, receipt.metadata) == []
    assert receipt.metadata["completion_contract_hash"] == completion_contract_hash(task)
    assert reviewer.payloads[0]["conditions"] == completion_conditions(task)
    assert reviewer.payloads[0]["task"]["completion_criteria"] == completion_conditions(task)
    assert len(receipt.metadata["criterion_results"]) == 1
    assert verify_task_receipt(stored, {**receipt.metadata, "criterion_results": []})
    changed_review = dict(receipt.metadata["criterion_results"][0], met=False)
    assert verify_task_receipt(stored, {
        **receipt.metadata, "criterion_results": [changed_review],
    })
    assert await AttackGraphService(world.graph_store).list_findings(verified=True) == []


@pytest.mark.parametrize("attribute", ["omit", "claim_all"])
async def test_model_cannot_omit_or_refute_a_condition_then_complete(attribute):
    reviewer = ContractReviewer()
    setattr(reviewer, attribute, attribute == "omit")
    world, runtime, path, _, identities, _ = await contracted_world(reviewer=reviewer)
    assert await complete(world, runtime, path, identities)
    assert (await world.tasks.get_task(runtime.active_task_id)).status == TaskStatus.RUNNING


async def test_changed_contract_rejects_late_review_and_old_receipt():
    world, runtime, path, reviewer, identities, task = await contracted_world()
    changed = task.model_copy(deep=True)
    changed.completion_criteria[0].min_evidence = 3

    async def mutate():
        await world.tasks.update_task(changed)

    reviewer.hook = mutate
    with pytest.raises(StateConflict):
        await complete(world, runtime, path, identities)
    receipts = [r for r in await world.evidence.list_for_run(runtime.run_id)
                if r.metadata.get("record_kind") == "task_completion_receipt"]
    assert receipts
    assert verify_task_receipt(changed, receipts[0].metadata)
    assert (await world.tasks.get_task(runtime.active_task_id)).status == TaskStatus.RUNNING


async def test_sqlite_roundtrip_v1_compatibility_v2_validation_and_old_receipt(tmp_path):
    world, runtime, path, _, identities, task = await contracted_world()
    store = SQLiteTaskGraphStore(tmp_path / "tasks.sqlite")
    mission = await world.tasks.get_mission(runtime.mission_id)
    await store.create_mission(mission)
    await store.create_task(task)
    reopened = SQLiteTaskGraphStore(tmp_path / "tasks.sqlite")
    restored = await reopened.get_task(task.task_id)
    assert restored == task
    assert completion_contract_hash(restored) == completion_contract_hash(task)
    assert await complete(world, runtime, path, identities) == []
    done = await world.tasks.get_task(task.task_id)
    receipt = (await world.evidence.get(done.completion_evidence_id)).metadata
    legacy_receipt = {k: v for k, v in receipt.items()
                      if not k.startswith("completion_contract") and k != "criterion_results"}
    assert verify_task_receipt(restored, legacy_receipt)
    legacy = MissionTask.model_validate({k: v for k, v in task.model_dump().items()
                                        if not k.startswith("completion_")})
    assert legacy.completion_contract_version == 1
    assert verify_task_receipt(legacy, legacy_receipt) == []
    for criteria in ([], [{"kind": "invented", "description": "not supported"}]):
        with pytest.raises(ValidationError):
            MissionTask.model_validate({**task.model_dump(), "completion_criteria": criteria})
