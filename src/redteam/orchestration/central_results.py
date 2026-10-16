"""Central Task result ownership, reusing original evidence and completion review.

Questions remain result items. An unresolved investigation can finish without
claiming positive criteria or a Finding. The owned planner settles the goal.
"""

from copy import deepcopy
from types import SimpleNamespace
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from redteam.core.contracts.events import EventType
from redteam.core.contracts.mission import InvestigationResult, ResultQuestion
from redteam.core.ids import EvidenceId
from redteam.orchestration.central_runtime import event, runtime_token
from redteam.orchestration.central_state import check_evidence
from redteam.orchestration.completion import (
    OriginProof,
    origin_row_hash,
    origin_task_changed_diagnostic,
)
from redteam.orchestration.completion_proposal import (
    CompletionProposal,
    record_unresolved,
    validate_unresolved,
)
from redteam.orchestration.coordination import StateConflict, content_hash
from redteam.orchestration.diagnostics import DecisionDiagnostic, safe_feedback
from redteam.orchestration.local_tools import LocalToolDescriptor, LocalToolResult

# An open EventEnvelope type; no schema/table or independent question state.
RESULT_UPDATED = "InvestigationResultUpdated"


class PublicationResultInput(InvestigationResult):
    """Model-facing publication input; fresh inputs default to the v2 contract.

    ``InvestigationResult`` remains the historical storage/read model with its v1
    default. Explicit v1 stays available for legacy Tasks; omitted wire versions
    use the current v2 result contract.
    """

    contract_version: Literal[1, 2] = Field(
        default=2,
        description="Defaults to 2; explicitly use 1 only for a legacy Task result.",
    )


class ResultPublication(BaseModel):
    model_config = ConfigDict(extra="forbid")
    result: PublicationResultInput


def probe_result_wire_schema() -> dict[str, Any]:
    """Expose the publication input schema with the probe's current v2 contract pinned.

    The detached schema preserves every result field and outcome while making the
    probe-purpose meaning of version 2 explicit to the model.
    """
    schema = deepcopy(PublicationResultInput.model_json_schema())
    properties = schema.get("properties")
    version = properties.get("contract_version") if isinstance(properties, dict) else None
    if not isinstance(version, dict):
        raise ValueError("publication_result_version_schema_missing")
    version.pop("enum", None)
    version["const"] = 2
    version["default"] = 2
    version["description"] = (
        "Version 2: the outcome describes this probe purpose, never "
        "the related candidate's verdict."
    )
    return schema


class OriginTaskChangedConflict(StateConflict):
    """A source-row CAS failure carrying its producer-owned safe diagnostic."""

    def __init__(self, origin_task_id: str, diagnostic: DecisionDiagnostic) -> None:
        self.origin_task_id = str(origin_task_id)
        self.diagnostic = diagnostic
        super().__init__(diagnostic.code)


def result_for(
    task, *, outcome, summary, evidence_ids, questions=(), receipt_id=None, hypothesis_ids=(),
    observations=(), limitations=(),
):
    """Retain durable investigation assertions and stable source references.

    Execution snapshots belong to immutable gap records, never these assertions:
    consumers project current waits from the execution ledger independently.
    """
    prior = task.result
    retained = {question.question_id: question for question in prior.questions} if prior else {}
    for question in questions:
        if question.question_id in retained and retained[question.question_id] != question:
            raise StateConflict("original_result_question_changed")
        retained[question.question_id] = question
    return InvestigationResult(
        result_id=prior.result_id if prior else "result_" + content_hash(str(task.task_id)),
        outcome=outcome,
        summary=summary,
        evidence_ids=list(dict.fromkeys([*(prior.evidence_ids if prior else []), *evidence_ids])),
        questions=list(retained.values()),
        hypothesis_ids=list(
            dict.fromkeys([*(prior.hypothesis_ids if prior else []), *hypothesis_ids,
                           *(task.related_hypothesis_ids
                             if task.investigation_contract_version == 2 else [])])
        ),
        completion_receipt_id=receipt_id,
        contract_version=task.investigation_contract_version,
        observations=list(dict.fromkeys([*(prior.observations if prior else []), *observations])),
        limitations=list(dict.fromkeys([*(prior.limitations if prior else []), *limitations])),
    )


async def commit_result(
    deps, runtime, task, result, token, *, final, validity_read=None, graph_proof=None,
    edge_proof=None, origin_proofs: tuple[OriginProof, ...] = (),
):
    checked = await check_evidence(
        deps.evidence_store,
        run_id=str(runtime.run_id),
        engagement_id=str(runtime.engagement_id),
        evidence_ids=[
            *map(str, result.evidence_ids),
            *(str(ref) for q in result.questions for ref in q.evidence_ids),
            *([str(result.completion_receipt_id)] if result.completion_receipt_id else []),
        ],
    )
    notification = event(
        deps.central_state,
        EventType.TASK_COMPLETED if final else RESULT_UPDATED,
        {"task_id": str(task.task_id), "result_id": result.result_id, "outcome": result.outcome},
    )

    def commit(tx):
        mission = tx.admit()
        current = tx.get_task(str(task.task_id))
        if (
            mission.revision != token[0]
            or content_hash(mission) != token[1]
            or content_hash(current) != content_hash(task)
        ):
            raise StateConflict("result_related_inputs_changed")
        for proof in origin_proofs:
            if proof.origin_task_id is None:
                continue
            current_origin = tx.get_task(proof.origin_task_id)
            if current_origin is None or origin_row_hash(current_origin) != proof.origin_task_hash:
                raise OriginTaskChangedConflict(
                    proof.origin_task_id,
                    origin_task_changed_diagnostic(
                        [proof.evidence_id], owner="orchestration.central_results",
                    ),
                )
        if validity_read is not None:
            validity_read.check(tx)
        if graph_proof is not None:
            from redteam.orchestration.result_claims import check_result_graph

            check_result_graph(tx, graph_proof)
        if edge_proof is not None:
            from redteam.orchestration.result_claims import check_result_edge_proof

            check_result_edge_proof(tx, edge_proof)
        if final:
            saved = tx.finish_task(
                str(task.task_id),
                result,
                owner=runtime.work_owner,
                version=runtime.task_ownership_version,
                checked=checked,
            )
        else:
            saved = tx.publish_result(
                str(task.task_id),
                result,
                owner=runtime.work_owner,
                version=runtime.task_ownership_version,
                expected_hash=content_hash(task),
                checked=checked,
            )
        tx.queue(notification)
        return saved

    saved = await deps.central_state.transaction(commit)
    await deps.event_bus.flush()
    return saved


async def publish_result_handler(tool_input, context):
    deps, runtime = context.deps, context.runtime
    if deps.central_state is None:
        return LocalToolResult(ok=False, error="central development selection required")
    token = await runtime_token(deps, runtime)
    publication = ResultPublication.model_validate(tool_input)
    result = publication.result
    task = await deps.central_state.get_task(str(runtime.active_task_id))
    if result.contract_version != task.investigation_contract_version:
        return LocalToolResult(ok=False, error="result contract does not match the current task")
    if (result.outcome == "confirmed"
            or (result.contract_version == 2 and result.outcome == "refuted")
            or result.finding_node_ids or result.completion_receipt_id):
        return LocalToolResult(
            ok=False, error=("reviewed probe outcomes require the existing settlement review"
                             if result.contract_version == 2 else
                             "positive results require the existing settlement review"),
        )
    from redteam.attack_graph.service import AttackGraphService

    service = AttackGraphService(deps.graph_store)
    for identity in result.hypothesis_ids:
        if await service.get_hypothesis(str(identity)) is None:
            return LocalToolResult(ok=False, error="result refers to an unknown hypothesis")
    merged = result_for(
        task,
        outcome=result.outcome,
        summary=result.summary,
        evidence_ids=result.evidence_ids,
        questions=result.questions,
        hypothesis_ids=result.hypothesis_ids,
        observations=result.observations,
        limitations=result.limitations,
    )
    if result.result_id != merged.result_id:
        # The producer chooses the first result identity; thereafter it is stable.
        if task.result is not None:
            return LocalToolResult(ok=False, error="original result identity changed")
        merged = merged.model_copy(update={"result_id": result.result_id})
    saved = await commit_result(deps, runtime, task, merged, token, final=False)
    return LocalToolResult(
        ok=True,
        payload={"task_id": str(saved.task_id), "result": saved.result.model_dump(mode="json")},
        refs=tuple(map(str, merged.evidence_ids)),
    )


def register_result_tool(registry):
    registry.register(
        LocalToolDescriptor(
            tool_id="task.publish_result",
            contribution_id="central-investigation-result",
            version="4", initial_exposure=True,
            description="Publish an intermediate investigation result and its follow-up questions "
            "while retaining the task. The result contract defaults to v2; explicitly use v1 "
            "only for a legacy Task. Cite preserved originals. Confirmed results require "
            "task.settle review. Probe contract v2 refutations also require review; outcomes "
            "describe this probe purpose, never the candidate verdict. Probe follow-ups may "
            "refer to the source task/result without a question; legacy follow-ups use the triple.",
            input_schema=ResultPublication.model_json_schema(),
        ),
        publish_result_handler,
    )


async def complete_reviewed_result(
    deps, runtime, task, token, receipt, proposal, *, validity_read, graph_proof,
    edge_proof=None, origin_proofs: tuple[OriginProof, ...] = (),
):
    """The existing positive origin/conditions/claims review has already succeeded."""
    result = result_for(
        task,
        outcome="confirmed" if proposal.outcome == "success" else "refuted",
        summary=proposal.reason or receipt.summary,
        evidence_ids=proposal.evidence_ids,
        receipt_id=receipt.evidence_id,
    )
    await commit_result(deps, runtime, task, result, token, final=True,
                        validity_read=validity_read, graph_proof=graph_proof,
                        edge_proof=edge_proof, origin_proofs=origin_proofs)


async def complete_unresolved_result(deps, runtime, task, token, proposal):
    """Shared public/native settlement; an investigation gap grants no success proof."""
    errors = await validate_unresolved(
        proposal, evidence_store=deps.evidence_store, runtime=runtime,
        target_node_ids=list(task.related_attack_graph_node_ids),
    )
    if errors:
        return errors
    gap_id = await record_unresolved(
        evidence_store=deps.evidence_store, runtime=runtime, proposal=proposal,
        path=None, task_id=str(task.task_id),
    )
    questions = [ResultQuestion(
        question_id="question_" + content_hash([str(task.task_id), question]),
        question=question, evidence_ids=[EvidenceId(ref) for ref in gap.evidence_ids],
    ) for gap in proposal.gaps for question in gap.unresolved_questions]
    result = result_for(
        task, outcome=proposal.outcome, summary=proposal.reason,
        evidence_ids=[*map(EvidenceId, proposal.evidence_ids), gap_id], questions=questions,
    )
    await commit_result(deps, runtime, task, result, token, final=True)
    runtime.unresolved_evidence_ids = list(dict.fromkeys([
        *runtime.unresolved_evidence_ids, gap_id,
    ]))
    return []


async def settle_central(scope, proposal: CompletionProposal, context):
    deps, runtime = context.deps, context.runtime
    if scope == "goal":
        return LocalToolResult(ok=False, error="goal settlement belongs to the owned main planner")
    token = await runtime_token(deps, runtime)
    task = await deps.central_state.get_task(str(runtime.active_task_id))
    if not proposal.unresolved:
        from redteam.orchestration.engine import InvestigationEngine
        from redteam.orchestration.semantic_completion import complete_task

        engine = InvestigationEngine(deps)
        engine._event_runtime = runtime
        errors = await complete_task(
            engine,
            None,
            SimpleNamespace(
                completion=proposal.model_dump(mode="json"),
                reason=proposal.reason,
            ),
            runtime,
            {},
            budget=context.budget,
        )
        if errors:
            return await completion_rejection(errors, proposal, task, deps, runtime)
    else:
        errors = await complete_unresolved_result(deps, runtime, task, token, proposal)
        if errors:
            return await completion_rejection(errors, proposal, task, deps, runtime)
    return LocalToolResult(
        ok=True,
        payload={"scope": scope, "outcome": proposal.outcome},
        refs=tuple(proposal.evidence_ids),
        runtime_outcome="task_completed",
    )


async def completion_rejection(errors, proposal, task, deps, runtime):
    """Keep producer diagnostics; unspecified legacy failures stay unknown.

    Publish scoped references only. A rejected reference is not made trustworthy
    by appearing in the diagnostic, nor is a diagnostic an execution original.
    """
    from redteam.core.contracts.completion import criterion_id

    evidence_ids = []
    for identity in dict.fromkeys(proposal.evidence_ids):
        record = await deps.evidence_store.get(identity)
        if (record is not None and str(record.run_id) == str(runtime.run_id)
                and str(record.engagement_id) == str(runtime.engagement_id)
                and (not record.metadata.get("mission_id")
                     or str(record.metadata["mission_id"]) == str(runtime.mission_id))):
            evidence_ids.append(identity)
    criterion_ids = [criterion_id(item) for item in task.completion_criteria]
    diagnostics = getattr(errors, "diagnostics", []) or [DecisionDiagnostic(
        code="completion_support_unproven", stage="completion_validation", kind="validation",
        cause_scope="unknown", owner="orchestration.semantic_completion",
        summary="Completion support was rejected; its cause is not yet classified.",
        repair_condition="Preserve originals and return an unresolved gap if no independent "
                         "support exists. Rereading does not repair internal contracts.",
    )]
    diagnostics = [item.model_copy(update={
        "evidence_ids": [ref for ref in (item.evidence_ids or evidence_ids)
                         if ref in evidence_ids],
        "criterion_ids": [ref for ref in (item.criterion_ids or criterion_ids)
                          if ref in criterion_ids],
    }).model_dump(mode="json") for item in diagnostics]
    return LocalToolResult(
        ok=False, error=safe_feedback("; ".join(errors)),
        payload={"diagnostics": diagnostics, "task_id": str(task.task_id),
                 "completion_accepted": False}, refs=tuple(evidence_ids),
    )
