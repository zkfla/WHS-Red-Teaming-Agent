"""Optional authentication comparison over preserved request evidence."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from redteam.attack_graph.identity import target_for_node
from redteam.attack_graph.predicates import Confidence, Provenance, facts_of
from redteam.attack_graph.service import AttackGraphService
from redteam.core.contracts.action import ActionProposal
from redteam.core.contracts.mission import redact_secret_material
from redteam.core.enums import EvidenceSourceType
from redteam.core.extensions import Contribution
from redteam.core.ids import EvidenceId, GraphNodeId
from redteam.event_bus.publisher import EventPublisher
from redteam.orchestration.authentication import (
    AuthenticationCheck,
    assess_preserved_authentication,
)
from redteam.orchestration.cognition import DecisionFailure, ReasoningHalted
from redteam.orchestration.completion import check_record_linkage
from redteam.orchestration.diagnostics import (
    diagnostic_for_exception,
    operational_exception_diagnostic,
)
from redteam.orchestration.events import AUTHENTICATION_CHECKED
from redteam.orchestration.evidence_access import raw_from_metadata
from redteam.orchestration.local_tools import (
    LocalToolContext,
    LocalToolDescriptor,
    LocalToolResult,
    runtime_tool_contribution,
)

AUTHENTICATION_ASSESS_TOOL_ID = "authentication.assess"


class AuthenticationAssessmentInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    baseline_evidence_id: str = Field(min_length=1)
    session_evidence_id: str = Field(min_length=1)
    session_ref: str = Field(min_length=1)
    expected_access: Literal["allow", "deny", "unspecified"] = "unspecified"


async def authentication_assess_handler(
    arguments: dict[str, Any], context: LocalToolContext,
) -> LocalToolResult:
    try:
        request = AuthenticationAssessmentInput.model_validate(arguments)
    except (ValueError, TypeError) as exc:
        diagnostic = diagnostic_for_exception(exc, stage="native_local_tool")
        return LocalToolResult(
            ok=False, error=diagnostic.feedback(),
            payload={"diagnostic": diagnostic.model_dump(mode="json")},
        )
    deps, runtime = context.deps, context.runtime
    try:
        records = []
        for identity in (request.baseline_evidence_id, request.session_evidence_id):
            record = await deps.evidence_store.get(EvidenceId(identity))
            if (
                record is None or record.run_id != runtime.run_id
                or record.engagement_id != runtime.engagement_id
                or record.source_type is not EvidenceSourceType.TOOL
            ):
                return LocalToolResult(
                    ok=False, error="authentication evidence unavailable for run",
                )
            raw = raw_from_metadata(record.metadata)
            if not isinstance(raw, dict) or check_record_linkage(
                record, record.metadata, raw, identity,
            ):
                return LocalToolResult(
                    ok=False, error="authentication evidence origin is incomplete",
                )
            records.append(record)
            proposal = ActionProposal.model_validate(record.metadata.get("proposal"))
            if proposal.run_id != record.run_id or proposal.engagement_id != record.engagement_id:
                return LocalToolResult(ok=False, error="authentication execution scope differs")
        current = records[1]
        check = AuthenticationCheck(
            baseline_evidence_id=EvidenceId(request.baseline_evidence_id),
            session_ref=request.session_ref, expected_access=request.expected_access,
        )
        assessment = await assess_preserved_authentication(check, current, deps.evidence_store)
        current_proposal = ActionProposal.model_validate(current.metadata.get("proposal"))
        public_assessment = assessment.model_dump(mode="json")
        # This is an HTTP response code, not session credential material.
        # Name the public observation by its value while retaining the shared
        # secret-key redactor for every reference and free-text field.
        public_assessment["response_status_code"] = public_assessment.pop("session_status")
        comparison = redact_secret_material({
            **public_assessment,
            "session_ref": request.session_ref,
            "expected_access": request.expected_access,
            "path": current_proposal.arguments.get("path"),
        })
        service = AttackGraphService(deps.graph_store)
        node = await service.get_node(GraphNodeId(current.metadata.get("target_node_id", "")))
        if node is None:
            return LocalToolResult(ok=False, error="authentication evidence target is unavailable")
        target = target_for_node(
            node.id, node.type, observed=facts_of(node.properties).get("observed_target"),
        )
        await service.observe_target(
            target, facts={"authentication_checks": [comparison]},
            provenance=Provenance(
                source="authentication.assess", run_id=str(runtime.run_id),
                evidence_id=str(current.evidence_id), confidence=Confidence.MEASURED,
                step=runtime.step_count,
            ), step=runtime.step_count,
        )
        event = await EventPublisher(
            deps.event_bus, run_id=runtime.run_id, engagement_id=runtime.engagement_id,
            source="orchestration.authentication",
        ).publish(
            AUTHENTICATION_CHECKED,
            {**comparison, "action_id": current.action_id, "hypothesis_id": None,
             "assessment_kind": "observation"},
            causation_id=context.last_event_id,
        )
        return LocalToolResult(
            ok=True, payload=comparison, refs=tuple(assessment.evidence_ids),
            last_event_id=event.event_id,
        )
    except (ReasoningHalted, DecisionFailure):
        raise
    except Exception as exc:
        diagnostic = operational_exception_diagnostic(
            exc, stage="native_local_tool", code="authentication_assessment_failed",
            summary="The preserved authentication observations could not be compared.",
        )
        return LocalToolResult(
            ok=False, error=diagnostic.feedback(),
            payload={"diagnostic": diagnostic.model_dump(mode="json")},
        )


def authentication_assess_descriptor() -> LocalToolDescriptor:
    return LocalToolDescriptor(
        tool_id=AUTHENTICATION_ASSESS_TOOL_ID,
        description=(
            "Compare a preserved anonymous HTTP baseline and selected session response. "
            "Provide both original evidence IDs and session_ref; expected_access is optional. "
            "Returns an authentication observation and source refs, never a confirmed finding. "
            "An inconclusive baseline comparison includes a fixed mismatch_reason naming "
            "the first failed source or request condition; correct the cited inputs before "
            "repeating the comparison. The baseline needs an explicit original anonymous receipt; "
            "the current response needs an original receipt matching its selected session. "
            "A successful tool call alone is not a resolved comparison. "
            "Submit any security claim through the supplied recording tool."
        ),
        input_schema=AuthenticationAssessmentInput.model_json_schema(),
        contribution_id="authentication-assessment",
        version="3",
    )


def authentication_assessment_contribution() -> Contribution:
    def factory(wiring: Any) -> tuple[LocalToolDescriptor, Any]:
        return authentication_assess_descriptor(), authentication_assess_handler

    return runtime_tool_contribution(
        contribution_id="authentication-assessment", version="3", factory=factory,
    )
