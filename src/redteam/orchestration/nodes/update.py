"""UPDATE — fold the outcome back into the Attack Graph.

Also runs for blocked actions (reached from POLICY as well as VERIFY):
nothing executed, but the attempt and the policy verdict are recorded.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from redteam.attack_graph.predicates import Confidence, Provenance
from redteam.core.contracts.events import EventType
from redteam.core.contracts.graph import Finding, Hypothesis
from redteam.core.enums import AgentPhase, PolicyVerdict
from redteam.orchestration.action_naming import canonical_action_key
from redteam.orchestration.context import RuntimeDeps
from redteam.orchestration.events import GRAPH_MUTATION_APPLIED, RESULT_MAPPER_FAILED
from redteam.orchestration.nodes._support import advance, graph_service, publisher_for
from redteam.orchestration.nodes.policy import denial_severity
from redteam.orchestration.runtime_observation import (
    RuntimeObservation,
    append_runtime_observation,
)
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
            if result is not None and state.get("result_evidence_id")
            else None
        ),
        confidence=Confidence.MEASURED if succeeded else Confidence.INFERRED,
    )

    # Planned attributes are never promoted into measured facts: only the
    # result normalizers below write domain facts. What the plan hoped for
    # stays in the plan, so a bare tool success cannot certify a predicate
    # nobody observed.
    observed_attrs: dict[str, Any] = {}
    facts: dict[str, object] = {
        "last_operation": operation_key,
        "last_operation_succeeded": succeeded,
        "last_tool_succeeded": tool_succeeded,
    }
    if provenance.evidence_id:
        facts["evidence_ids"] = [provenance.evidence_id]
    if blocked:
        # A policy block is a *decision*, not an execution failure: nothing ran,
        # so UPDATE must never touch ``consecutive_failures`` / retry counters
        # (it does not -- those are owned by EXECUTE/VERIFY). It only records
        # what was blocked and why, including Control's reaction hint.
        facts["last_operation_blocked"] = True
        # Structured markers stay; the free-text reasons travel on the event below.
        facts["last_block_verdict"] = decision.verdict.value if decision else None
        block_severity = denial_severity(decision)
        if block_severity is not None:
            facts["last_block_severity"] = block_severity

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
            "operation": operation_key,
            "attrs": observed_attrs,
            "blocked": blocked,
            "block_verdict": decision.verdict.value if (blocked and decision) else None,
            "block_reasons": list(decision.reasons) if (blocked and decision) else [],
            "verification_failed": verification_failed,
            "succeeded": succeeded,
        },
        correlation_id=correlation_id,
        causation_id=state.get("last_event_id"),
    )

    # Tool Result -> Attack Graph Mutation (SERVICE/ENDPOINT discovery, if
    # a mapper is registered for this tool_id): the only writer of domain
    # facts about discovered nodes, and it never raises
    # -- `ResultMapperRegistry.map_result_with_outcome` degrades to "nothing
    # new" for an unmapped tool_id, a failed/blocked action, or malformed
    # output. The outcome (mapped/empty/no_mapper/failed) is computed for
    # every preserved result so failures stay visible; graph writes below
    # stay success-gated and never promote unmeasured output.
    mutated_nodes = 0
    mutated_edges = 0
    mutated_findings = 0
    if result is not None and step is not None:
        outcome = deps.result_mappers.map_result_with_outcome(
            tool_id=result.tool_id,
            target=proposal.target,
            arguments=proposal.arguments,
            result=result,
        )
        mutation = outcome.mutation
        if succeeded:
            mutated_nodes = len(mutation.nodes)
            mutated_edges = len(mutation.edges)
            mutated_findings = len(mutation.findings)
        # Call-local failure read: the shared last_failure slot stays only
        # for the legacy map_result wrapper, new consumers use the outcome.
        if outcome.status == "failed":
            try:
                from redteam.orchestration.recovery import record_mapping_failure

                await record_mapping_failure(
                    evidence_store=deps.evidence_store,
                    run_id=runtime.run_id,
                    engagement_id=runtime.engagement_id,
                    source_evidence_id=(
                        str(state["result_evidence_id"])
                        if state.get("result_evidence_id")
                        else None
                    ),
                    tool_id=str(result.tool_id),
                    mapper=outcome.mapper or "unknown",
                    reason=outcome.reason or "unknown",
                    action_id=proposal.action_id,
                    invocation_id=result.invocation_id,
                    hypothesis_id=proposal.hypothesis_id,
                    attempt=1,
                )
            except Exception:  # noqa: BLE001 - the audit note must not break the update
                pass
            failed_event = await publisher.publish(
                RESULT_MAPPER_FAILED,
                {
                    "operation": proposal.operation,
                    "tool_id": outcome.tool_id,
                    "mapper": outcome.mapper,
                    "reason": outcome.reason,
                    "action_id": str(proposal.action_id),
                    "invocation_id": str(result.invocation_id),
                },
                correlation_id=correlation_id,
                causation_id=last_event.event_id,
            )
            last_event = failed_event

        if mutation.target_references:
            try:
                from redteam.orchestration.observations.reference_scan import (
                    is_reference_grounded,
                )
                from redteam.orchestration.recovery import record_target_references

                source_evidence_id = (
                    str(state["result_evidence_id"]) if state.get("result_evidence_id") else None
                )
                if source_evidence_id is not None:
                    tool_result_json = result.model_dump(mode="json")
                    grounded = []
                    for entry in mutation.target_references:
                        draft = entry
                        if draft.source_evidence_id is None:
                            draft = draft.model_copy(
                                update={"source_evidence_id": source_evidence_id}
                            )
                        if is_reference_grounded(
                            draft,
                            tool_result=tool_result_json,
                            action_id=str(proposal.action_id),
                            invocation_id=str(result.invocation_id),
                            evidence_id=source_evidence_id,
                        ):
                            grounded.append(draft)
                    if grounded:
                        await record_target_references(
                            evidence_store=deps.evidence_store,
                            run_id=runtime.run_id,
                            engagement_id=runtime.engagement_id,
                            source_evidence_id=source_evidence_id,
                            tool_id=str(result.tool_id),
                            references=[item.model_dump(mode="json") for item in grounded],
                            action_id=proposal.action_id,
                            invocation_id=result.invocation_id,
                            hypothesis_id=proposal.hypothesis_id,
                            attempt=1,
                        )
            except Exception:  # noqa: BLE001 - references must not break the update
                pass

        # Graph writes stay success-gated: a verification or tool failure
        # still records the mapping outcome and grounded references above,
        # but never promotes unmeasured output into nodes/edges/findings.
        nodes_to_apply = mutation.nodes if succeeded else []
        edges_to_apply = mutation.edges if succeeded else []
        findings_to_apply = mutation.findings if succeeded else []
        # Processing record: before-attempt intent, confirmed counters after
        # each write phase, and a partial/failed companion on exception. The
        # original evidence record is never touched. Skipped calls produce no
        # companion here: their mapping outcome already travels on the verify
        # metadata, so nothing masquerades a skip as work done.
        from redteam.orchestration.recovery import (
            ProcessingAttempt,
            record_processing_attempt,
        )

        # Only actual write attempts produce processing records: skipped
        # calls already expose their mapping outcome on the verify metadata,
        # so no companion here masquerades a skip as work done.
        findings_planned = findings_to_apply if provenance.evidence_id else []
        writes_planned = bool(nodes_to_apply or edges_to_apply or findings_planned)
        processing_source_evidence_id = (
            str(state["result_evidence_id"]) if state.get("result_evidence_id") else None
        )
        confirmed_nodes = 0
        confirmed_edges = 0
        confirmed_findings = 0
        processing_stage: str | None = None
        processing_failed_at: str | None = None
        if writes_planned:
            try:
                await record_processing_attempt(
                    evidence_store=deps.evidence_store,
                    run_id=runtime.run_id,
                    engagement_id=runtime.engagement_id,
                    source_evidence_id=processing_source_evidence_id,
                    tool_id=str(result.tool_id),
                    processing=ProcessingAttempt(
                        status="attempted",
                        attempted_nodes=len(nodes_to_apply),
                        attempted_edges=len(edges_to_apply),
                        attempted_findings=len(findings_planned),
                    ),
                    action_id=proposal.action_id,
                    invocation_id=result.invocation_id,
                    hypothesis_id=proposal.hypothesis_id,
                    attempt=1,
                )
            except Exception:  # noqa: BLE001 - the attempt note must not break the update
                pass
        processing_failed_at = "nodes"
        try:
            for node_mutation in nodes_to_apply:
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
                    facts={
                        **observed_facts,
                        **(
                            {"evidence_ids": [provenance.evidence_id]}
                            if provenance.evidence_id
                            else {}
                        ),
                    },
                    labels=node_mutation.labels,
                    provenance=provenance,
                    step=runtime.step_count,
                )
                confirmed_nodes += 1
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
            processing_stage = "nodes"
            processing_failed_at = "edges"
            for edge_mutation in edges_to_apply:
                await service.link(
                    edge_mutation.edge_type,
                    edge_mutation.source_id,
                    edge_mutation.target_id,
                    edge_mutation.properties,
                    provenance=provenance,
                )
                confirmed_edges += 1

            processing_stage = "edges"
            if provenance.evidence_id:
                processing_failed_at = "findings"
                for finding_mutation in findings_to_apply:
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
                    confirmed_findings += 1
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

            processing_stage = "findings"
            if succeeded and (mutation.nodes or mutation.edges or mutation.findings):
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

                processing_stage = "done"
                if writes_planned:
                    try:
                        await record_processing_attempt(
                            evidence_store=deps.evidence_store,
                            run_id=runtime.run_id,
                            engagement_id=runtime.engagement_id,
                            source_evidence_id=processing_source_evidence_id,
                            tool_id=str(result.tool_id),
                            processing=ProcessingAttempt(
                                status="confirmed",
                                stage="done",
                                attempted_nodes=len(nodes_to_apply),
                                attempted_edges=len(edges_to_apply),
                                attempted_findings=len(findings_planned),
                                confirmed_nodes=confirmed_nodes,
                                confirmed_edges=confirmed_edges,
                                confirmed_findings=confirmed_findings,
                            ),
                            action_id=proposal.action_id,
                            invocation_id=result.invocation_id,
                            hypothesis_id=proposal.hypothesis_id,
                            attempt=1,
                        )
                    except Exception:  # noqa: BLE001 - the companion must not break the update
                        pass
        except Exception as exc:  # noqa: BLE001 - partial state is recorded, then the throw stands
            if writes_planned:
                try:
                    await record_processing_attempt(
                        evidence_store=deps.evidence_store,
                        run_id=runtime.run_id,
                        engagement_id=runtime.engagement_id,
                        source_evidence_id=processing_source_evidence_id,
                        tool_id=str(result.tool_id),
                        processing=ProcessingAttempt(
                            status=(
                                "partial"
                                if (confirmed_nodes or confirmed_edges or confirmed_findings)
                                else "failed"
                            ),
                            stage=processing_stage,
                            attempted_nodes=len(nodes_to_apply),
                            attempted_edges=len(edges_to_apply),
                            attempted_findings=len(findings_planned),
                            confirmed_nodes=confirmed_nodes,
                            confirmed_edges=confirmed_edges,
                            confirmed_findings=confirmed_findings,
                            failed_at=processing_failed_at,
                            reason=type(exc).__name__,
                        ),
                        action_id=proposal.action_id,
                        invocation_id=result.invocation_id,
                        hypothesis_id=proposal.hypothesis_id,
                        attempt=1,
                    )
                except Exception:  # noqa: BLE001 - the companion must not mask the failure
                    pass
            raise

    work_id = state.get("current_work_id")
    approval_waiting = decision is not None and decision.verdict is PolicyVerdict.REQUIRE_APPROVAL
    if work_id is not None and approval_waiting:
        await deps.work_store.release(
            work_id, owner=str(runtime.run_id), now=datetime.now(timezone.utc)
        )
    elif work_id is not None:
        error_code = result.error.code if result is not None and result.error else None
        invalidated = (
            blocked
            and decision is not None
            and "orchestration:stale_or_invalid_path" in (decision.matched_rules or [])
        ) or error_code in {"tool_binding_invalid", "lease_lost_before_execution"}
        if invalidated:
            work_status = WorkStatus.STALE
            reason = "invalidated_before_execution"
        elif blocked:
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
                reason = error_code if error_code else "tool_failed"
        await deps.work_store.complete(
            work_id,
            owner=str(runtime.run_id),
            status=work_status,
            now=datetime.now(timezone.utc),
            reason=reason,
        )

    state_outcome = "blocked" if blocked else "updated" if succeeded else "failed"
    next_runtime = append_runtime_observation(
        runtime.model_copy(update={"phase": AgentPhase.REPLAN}),
        RuntimeObservation(
            kind="state",
            summary=(
                f"Attack graph {state_outcome} for {node.type.value} {node.id}; "
                f"added {mutated_nodes} node(s), {mutated_edges} edge(s), "
                f"and {mutated_findings} finding(s)"
            )[:500],
            outcome=state_outcome,
            step=runtime.step_count,
            action_id=str(proposal.action_id),
            capability=proposal.capability,
            operation=proposal.operation,
            target=str(node.id),
            reasons=(list(decision.reasons[:8]) if blocked and decision is not None else []),
            alternative_allowed=True,
        ),
    )

    return advance(
        state,
        next_runtime,
        last_event_id=last_event.event_id,
        current_work_id=work_id if approval_waiting else None,
    )
