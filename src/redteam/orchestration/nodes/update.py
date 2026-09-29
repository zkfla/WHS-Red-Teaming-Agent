"""UPDATE — fold the outcome back into the Attack Graph.

Also runs for blocked actions (reached from POLICY as well as VERIFY):
nothing executed, but the attempt and the policy verdict are recorded.
"""

from __future__ import annotations

from datetime import datetime, timezone

from redteam.attack_graph.predicates import Confidence, Provenance
from redteam.core.contracts.events import EventType
from redteam.core.contracts.graph import Finding, Hypothesis
from redteam.core.enums import AgentPhase
from redteam.orchestration.action_naming import canonical_action_key
from redteam.orchestration.context import RuntimeDeps
from redteam.orchestration.events import GRAPH_MUTATION_APPLIED, RESULT_MAPPER_FAILED
from redteam.orchestration.nodes._support import advance, graph_service, publisher_for
from redteam.orchestration.state import GraphState
from redteam.orchestration.work.models import WorkStatus


async def update_node(state: GraphState, *, deps: RuntimeDeps) -> GraphState:
    runtime = state["runtime"]
    proposal = state["action_proposal"]
    result = state["tool_result"]
    decision = state.get("policy_decision")
    step = state.get("plan_step")
    assert proposal is not None

    service = graph_service(deps)
    publisher = publisher_for(state, deps, "orchestration.update")
    correlation_id = str(proposal.action_id)
    operation_key = canonical_action_key(proposal.capability, proposal.operation)
    blocked = result is None
    tool_succeeded = result is not None and result.success
    verification_exception = (
        result.metadata.get("verification_exception") if result is not None else None
    )
    verification_failed = bool(verification_exception)
    succeeded = tool_succeeded and not verification_failed

    # PLAN records the hypothesis before the policy gate so the question is
    # auditable. A blocked action never reaches VERIFY, therefore UPDATE must
    # close it explicitly or reporting remains permanently blocked by a fake
    # "open" hypothesis that was never tested.
    if blocked and proposal.hypothesis_id is not None:
        await service.resolve_hypothesis(
            str(proposal.hypothesis_id),
            status="inconclusive",
            step=runtime.step_count,
        )

    provenance = Provenance(
        step=runtime.step_count,
        source=f"tool:{result.tool_id}" if result is not None else "control-plane",
        run_id=str(runtime.run_id),
        evidence_id=(
            str(state["result_evidence_id"])
            if result is not None and state.get("result_evidence_id") else None
        ),
        confidence=Confidence.MEASURED if succeeded else Confidence.INFERRED,
    )

    # Only a successful execution may assert a predicate: recording
    # "reachable" after a failed or blocked probe would make the graph
    # claim something nobody observed.
    observed_attrs = dict(step.observed_attrs) if step is not None and succeeded else {}
    facts: dict[str, object] = {
        "last_operation": operation_key,
        "last_operation_succeeded": succeeded,
        "last_tool_succeeded": tool_succeeded,
    }
    if provenance.evidence_id:
        facts["evidence_ids"] = [provenance.evidence_id]
    if verification_failed:
        facts["last_verification_failed"] = True
    if blocked:
        facts["last_operation_blocked"] = True
        facts["last_block_verdict"] = decision.verdict.value if decision else None
        facts["last_block_reasons"] = list(decision.reasons) if decision else []

    node = await service.observe_target(
        proposal.target,
        attrs=observed_attrs,
        facts=facts,
        labels=[f"observed-by:{proposal.agent_id}"],
        provenance=provenance,
        step=runtime.step_count,
    )

    attempt_record_key = step.attempt_record_key if step is not None else operation_key
    await service.record_attempt(
        node.id, attempt_record_key, step=runtime.step_count, provenance=provenance
    )

    last_event = await publisher.publish(
        EventType.GRAPH_UPDATED,
        {
            "node_id": str(node.id),
            "node_type": node.type.value,
            "action_id": proposal.action_id,
            "attrs": observed_attrs,
            "blocked": blocked,
        },
        correlation_id=correlation_id,
        causation_id=state.get("last_event_id"),
    )

    # Tool Result -> Attack Graph Mutation (SERVICE/ENDPOINT discovery, if
    # a mapper is registered for this tool_id): purely additive, never
    # replaces the static `observed_attrs` write above, and never raises
    # -- `ResultMapperRegistry.map_result` degrades to "nothing new" for
    # an unmapped tool_id, a failed/blocked action, or malformed output.
    if succeeded and result is not None and step is not None:
        mutation = deps.result_mappers.map_result(
            tool_id=result.tool_id,
            target=proposal.target,
            arguments=proposal.arguments,
            result=result,
        )
        mapper_failure = deps.result_mappers.last_failure
        if mapper_failure is not None:
            failed_event = await publisher.publish(
                RESULT_MAPPER_FAILED,
                {
                    "operation": proposal.operation,
                    "tool_id": mapper_failure.tool_id,
                    "mapper": mapper_failure.mapper,
                    "reason": mapper_failure.reason,
                    "action_id": str(proposal.action_id),
                    "invocation_id": str(result.invocation_id),
                },
                correlation_id=correlation_id,
                causation_id=last_event.event_id,
            )
            last_event = failed_event

        for node_mutation in mutation.nodes:
            observed_facts = dict(node_mutation.facts)
            if node_mutation.observations and provenance.evidence_id:
                observed_facts["observations"] = [
                    {**item, "evidence_id": provenance.evidence_id}
                    for item in node_mutation.observations
                ]
            discovered = await service.upsert_node(
                node_mutation.node_type,
                node_id=node_mutation.node_id,
                attrs=node_mutation.attrs,
                facts={**observed_facts, **(
                    {"evidence_ids": [provenance.evidence_id]} if provenance.evidence_id else {}
                )},
                labels=node_mutation.labels,
                provenance=provenance,
                step=runtime.step_count,
            )
            last_event = await publisher.publish(
                EventType.GRAPH_UPDATED,
                {
                    "node_id": str(discovered.id),
                    "node_type": discovered.type.value,
                    "action_id": proposal.action_id,
                    "attrs": node_mutation.attrs,
                    "blocked": False,
                },
                correlation_id=correlation_id,
                causation_id=last_event.event_id,
            )
        for edge_mutation in mutation.edges:
            await service.link(
                edge_mutation.edge_type,
                edge_mutation.source_id,
                edge_mutation.target_id,
                edge_mutation.properties,
                provenance=provenance,
            )

        if provenance.evidence_id:
            for finding_mutation in mutation.findings:
                existing = await service.get_finding(finding_mutation.finding_id)
                evidence_ids = sorted(
                    {
                        provenance.evidence_id,
                        *(existing.evidence_ids if existing is not None else []),
                    }
                )
                hypothesis = Hypothesis(
                    hypothesis_id=finding_mutation.hypothesis_id,
                    description=finding_mutation.expectation.description,
                    confidence=1.0,
                    status="confirmed",
                    metadata={
                        "check_id": finding_mutation.check_id,
                        "expectation": finding_mutation.expectation.model_dump(mode="json"),
                        "verdict": finding_mutation.verdict.value,
                        "reason": finding_mutation.reason,
                        "evidence_ids": evidence_ids,
                        "target_node_id": str(finding_mutation.target_node_id),
                    },
                )
                await service.record_hypothesis(
                    hypothesis,
                    about=finding_mutation.target_node_id,
                    step=runtime.step_count,
                    provenance=provenance,
                )
                finding = Finding(
                    finding_id=finding_mutation.finding_id,
                    title=finding_mutation.title,
                    description=finding_mutation.description,
                    severity=finding_mutation.severity,
                    verified=True,
                    evidence_ids=evidence_ids,
                    metadata={
                        **finding_mutation.metadata,
                        "hypothesis_id": finding_mutation.hypothesis_id,
                        "target": str(finding_mutation.target_node_id),
                        "expectation": finding_mutation.expectation.model_dump(mode="json"),
                        "verdict": finding_mutation.verdict.value,
                        "verification_reason": finding_mutation.reason,
                    },
                )
                await service.record_finding(
                    finding,
                    hypothesis_id=finding_mutation.hypothesis_id,
                    about=finding_mutation.target_node_id,
                    step=runtime.step_count,
                    provenance=provenance,
                )
                if existing is None:
                    created = await publisher.publish(
                        EventType.FINDING_CREATED,
                        {
                            "finding_id": finding.finding_id,
                            "title": finding.title,
                            "severity": finding.severity,
                            "evidence_ids": finding.evidence_ids,
                            "hypothesis_id": finding_mutation.hypothesis_id,
                            "action_id": proposal.action_id,
                            "candidate_key": step.candidate_key,
                        },
                        correlation_id=correlation_id,
                        causation_id=last_event.event_id,
                    )
                    last_event = await publisher.publish(
                        EventType.FINDING_VERIFIED,
                        {
                            "finding_id": finding.finding_id,
                            "reason": finding_mutation.reason,
                        },
                        correlation_id=correlation_id,
                        causation_id=created.event_id,
                    )

        if mutation.nodes or mutation.edges or mutation.findings:
            last_event = await publisher.publish(
                GRAPH_MUTATION_APPLIED,
                {
                    "action_id": str(proposal.action_id),
                    "tool_id": str(result.tool_id),
                    "nodes": len(mutation.nodes),
                    "edges": len(mutation.edges),
                    "findings": len(mutation.findings),
                },
                correlation_id=correlation_id,
                causation_id=last_event.event_id,
            )

    work_id = state.get("current_work_id")
    if work_id is not None:
        if blocked:
            work_status = WorkStatus.BLOCKED
            reason = decision.verdict.value if decision is not None else "blocked"
        elif succeeded:
            work_status = WorkStatus.SUCCEEDED
            reason = None
        else:
            work_status = WorkStatus.FAILED
            if verification_failed:
                reason = f"verification_exception:{verification_exception}"
            else:
                reason = result.error.code if result is not None and result.error else "tool_failed"
        await deps.work_store.complete(
            work_id,
            owner=str(runtime.run_id),
            status=work_status,
            now=datetime.now(timezone.utc),
            reason=reason,
        )

    return advance(
        state,
        runtime.model_copy(update={"phase": AgentPhase.REPLAN}),
        last_event_id=last_event.event_id,
        current_work_id=None,
    )
