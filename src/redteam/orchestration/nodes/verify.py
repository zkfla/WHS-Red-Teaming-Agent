"""VERIFY — append Evidence, then reach a ``Verdict`` against the
expectation recorded before the action ran. A VIOLATION produces a
verified ``Finding`` carrying the ``EvidenceId`` behind it.
"""

from __future__ import annotations

from redteam.attack_graph.identity import node_id_for_target
from redteam.attack_graph.predicates import Confidence, Provenance
from redteam.core.contracts.events import EventType
from redteam.core.contracts.graph import Finding
from redteam.core.convert import tool_result_to_evidence
from redteam.core.enums import AgentPhase, Verdict
from redteam.core.ids import new_id
from redteam.orchestration.context import RuntimeDeps
from redteam.orchestration.expectations import verdict_for
from redteam.orchestration.nodes._support import advance, graph_service, publisher_for
from redteam.orchestration.service_status import (
    HYPOTHESIS_STATUS_BY_VERDICT,
    severity_for,
)
from redteam.orchestration.state import GraphState


async def verify_node(state: GraphState, *, deps: RuntimeDeps) -> GraphState:
    runtime = state["runtime"]
    proposal = state["action_proposal"]
    result = state["tool_result"]
    step = state.get("plan_step")
    assert proposal is not None and result is not None

    correlation_id = str(proposal.action_id)
    publisher = publisher_for(state, deps, "orchestration.verify")
    service = graph_service(deps)

    evidence = tool_result_to_evidence(
        result,
        run_id=runtime.run_id,
        engagement_id=runtime.engagement_id,
        hypothesis_id=proposal.hypothesis_id,
        summary=proposal.expected_observation,
    )
    await deps.evidence_store.append(evidence)

    created = await publisher.publish(
        EventType.EVIDENCE_CREATED,
        {
            "evidence_id": evidence.evidence_id,
            "action_id": proposal.action_id,
            "invocation_id": evidence.invocation_id,
            "hypothesis_id": proposal.hypothesis_id,
        },
        correlation_id=correlation_id,
        causation_id=state.get("last_event_id"),
    )
    last_event_id = created.event_id

    expectation = step.expectation if step is not None else None
    verdict, reason = verdict_for(expectation, result)

    provenance = Provenance(
        step=runtime.step_count,
        source=f"tool:{result.tool_id}",
        evidence_id=str(evidence.evidence_id),
        run_id=str(runtime.run_id),
        confidence=Confidence.MEASURED,
    )

    if proposal.hypothesis_id is not None:
        await service.resolve_hypothesis(
            str(proposal.hypothesis_id),
            status=HYPOTHESIS_STATUS_BY_VERDICT[verdict],
            step=runtime.step_count,
        )

    if verdict is Verdict.VIOLATION:
        finding = Finding(
            finding_id=new_id("find"),
            title=f"{proposal.capability}/{proposal.operation} confirmed a violation",
            description=reason,
            severity=severity_for(step),
            verified=True,
            evidence_ids=[str(evidence.evidence_id)],
            metadata={
                "action_id": str(proposal.action_id),
                "agent_id": str(proposal.agent_id),
                "hypothesis_id": str(proposal.hypothesis_id) if proposal.hypothesis_id else None,
                "target": str(node_id_for_target(proposal.target)),
            },
        )
        await service.record_finding(
            finding,
            hypothesis_id=str(proposal.hypothesis_id) if proposal.hypothesis_id else None,
            about=node_id_for_target(proposal.target),
            step=runtime.step_count,
            provenance=provenance,
        )
        finding_created = await publisher.publish(
            EventType.FINDING_CREATED,
            {
                "finding_id": finding.finding_id,
                "severity": finding.severity,
                "evidence_ids": finding.evidence_ids,
            },
            correlation_id=correlation_id,
            causation_id=last_event_id,
        )
        finding_verified = await publisher.publish(
            EventType.FINDING_VERIFIED,
            {"finding_id": finding.finding_id, "reason": reason},
            correlation_id=correlation_id,
            causation_id=finding_created.event_id,
        )
        last_event_id = finding_verified.event_id

    return advance(
        state,
        runtime.model_copy(update={"phase": AgentPhase.UPDATE, "last_verdict": verdict}),
        last_event_id=last_event_id,
    )
