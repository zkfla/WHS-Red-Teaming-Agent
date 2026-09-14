"""VERIFY node — normalizes the ``ToolResult`` into an ``EvidenceRecord``.

Uses ``redteam.core.convert.tool_result_to_evidence`` (mechanical
conversion, shared with ``redteam.evidence.service.EvidenceService``)
and writes through ``deps.evidence_store`` (a ``core`` port) directly
rather than importing Control Team's ``EvidenceService`` convenience
layer.

TODO(agent-team): real verification logic. ``last_verdict`` below is a
placeholder mapping (tool succeeded -> PASS, otherwise UNKNOWN) — see
``redteam.core.enums.Verdict``'s docstring for why that's *not* the
same axis as ``result.success`` and must not be treated as one once
real hypothesis verification exists. No new "VerificationResult"-style
contract is needed to fix this placeholder — reuse ``Verdict`` +
richer logic here. ``FindingCreated``/``FindingVerified`` aren't
emitted for the same reason (no real Finding logic yet).
"""

from __future__ import annotations

from datetime import datetime, timezone

from redteam.core.contracts.events import EventEnvelope, EventType
from redteam.core.convert import tool_result_to_evidence
from redteam.core.enums import AgentPhase, Verdict
from redteam.core.ids import EventId, new_id
from redteam.orchestration.context import RuntimeDeps
from redteam.orchestration.state import GraphState


async def verify_node(state: GraphState, *, deps: RuntimeDeps) -> GraphState:
    runtime = state["runtime"]
    proposal = state["action_proposal"]
    result = state["tool_result"]
    assert proposal is not None and result is not None

    evidence = tool_result_to_evidence(
        result,
        run_id=runtime.run_id,
        engagement_id=runtime.engagement_id,
        hypothesis_id=proposal.hypothesis_id,
        summary=proposal.expected_observation,
    )
    await deps.evidence_store.append(evidence)

    await deps.event_bus.publish(
        EventEnvelope(
            event_id=EventId(new_id("evt")),
            event_type=EventType.EVIDENCE_CREATED,
            run_id=runtime.run_id,
            engagement_id=runtime.engagement_id,
            source="orchestration.verify",
            timestamp=datetime.now(timezone.utc),
            payload={"evidence_id": evidence.evidence_id, "action_id": proposal.action_id},
        )
    )

    verdict = Verdict.PASS if result.success else Verdict.UNKNOWN
    new_runtime = runtime.model_copy(update={"phase": AgentPhase.UPDATE, "last_verdict": verdict})
    return {**state, "runtime": new_runtime}
