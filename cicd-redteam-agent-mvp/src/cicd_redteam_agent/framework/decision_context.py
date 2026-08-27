from __future__ import annotations

from collections import Counter
from typing import Any

from cicd_redteam_agent.domain.models import AnalysisState
from cicd_redteam_agent.security.redaction import redact

RECENT_EVENT_LIMIT = 7
MESSAGE_CHARACTER_LIMIT = 12_000
INSTRUCTION_CHARACTER_LIMIT = 16_000


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _bounded_text(value: Any, limit: int) -> str:
    text = redact(str(value or ""))
    if len(text) <= limit:
        return text
    head_size = int(limit * 0.7)
    tail_size = limit - head_size
    omitted = len(text) - limit
    return (
        f"{text[:head_size]}\n"
        f"...[projection omitted {omitted} characters]...\n"
        f"{text[-tail_size:]}"
    )


def _project_tool_calls(message: Any) -> list[dict[str, str]]:
    projected: list[dict[str, str]] = []
    for tool_call in _field(message, "tool_calls", []) or []:
        function = _field(tool_call, "function", {})
        projected.append(
            {
                "id": str(_field(tool_call, "id", "")),
                "name": str(_field(function, "name", "")),
                "arguments": _bounded_text(_field(function, "arguments", ""), 4_000),
            }
        )
    return projected


def _project_message(message: Any) -> dict[str, Any]:
    projected: dict[str, Any] = {
        "role": str(_field(message, "role", "user")),
        "content": _bounded_text(_field(message, "content", ""), MESSAGE_CHARACTER_LIMIT),
    }
    tool_call_id = _field(message, "tool_call_id")
    if tool_call_id:
        projected["tool_call_id"] = str(tool_call_id)
    tool_calls = _project_tool_calls(message)
    if tool_calls:
        projected["tool_calls"] = tool_calls
    return projected


def _count_relations(state: AnalysisState) -> dict[str, int]:
    counts = Counter(
        f"{relation.category or 'uncategorized'}:{relation.verdict.value}"
        for relation in state.relations
    )
    return dict(sorted(counts.items()))


def _current_outputs(state: AnalysisState) -> dict[str, list[dict[str, Any]]]:
    return {
        "findings": [
            {
                "finding_id": finding.finding_id,
                "rule_id": finding.rule_id,
                "status": finding.status.value,
                "evidence_ids": finding.evidence_ids,
                "missing_evidence": finding.missing_evidence,
                "summary": _bounded_text(finding.summary, 1_000),
            }
            for finding in state.findings[-20:]
        ],
        "attack_paths": [
            {
                "path_id": path.path_id,
                "status": path.status.value,
                "nodes": path.nodes,
                "relation_ids": [edge.relation_id for edge in path.edges],
                "missing_evidence": path.missing_evidence,
            }
            for path in state.paths[-20:]
        ],
        "cloud_identities": [
            {
                "identity_id": identity.identity_id,
                "provider": identity.provider,
                "mechanism": identity.mechanism,
                "workflow_id": identity.workflow_id,
                "job_id": identity.job_id,
                "attacker_control": identity.attacker_control.value,
                "trust_verified": identity.trust_verified.value,
            }
            for identity in state.identities[-20:]
        ],
    }


def build_analysis_state_projection(state: AnalysisState) -> dict[str, Any]:
    repeated_total = sum(max(0, count - 1) for count in state.repeated_calls.values())
    return {
        "repository": {
            "repository_id": state.repository.repository_id,
            "git_commit": state.repository.git_commit,
            "dirty": state.repository.dirty,
        },
        "counts": {
            "workflows": len(state.workflows),
            "evidence": len(state.evidence),
            "relations": len(state.relations),
            "assessments": len(state.assessments),
            "findings": len(state.findings),
            "attack_paths": len(state.paths),
            "cloud_identities": len(state.identities),
            "secret_references": len(state.secret_references),
        },
        "relation_counts": _count_relations(state),
        "pending_questions": [
            _bounded_text(question, 1_000) for question in state.pending_questions[-20:]
        ],
        "recently_inspected_resources": [
            _bounded_text(resource, 1_000) for resource in state.inspected_resources[-50:]
        ],
        "call_progress": {
            "unique_call_signatures": len(state.repeated_calls),
            "repeated_calls": repeated_total,
        },
        "working_memory": state.working_memory.model_dump(mode="json"),
        "current_outputs": _current_outputs(state),
    }


def build_decision_context(
    history: list[Any], state: AnalysisState, *, recent_event_limit: int = RECENT_EVENT_LIMIT
) -> dict[str, Any]:
    """Project canonical Agent state into a bounded, provider-neutral model input."""

    system_messages = [message for message in history if _field(message, "role") == "system"]
    non_system_messages = [
        message
        for message in history
        if _field(message, "role") != "system"
        and _field(message, "name") != "agent_state_projection"
    ]
    recent_messages = non_system_messages[-max(1, recent_event_limit) :]

    return {
        "schema_version": "1.0",
        "trusted_agent_instructions": _bounded_text(
            _field(system_messages[0], "content", "") if system_messages else "",
            INSTRUCTION_CHARACTER_LIMIT,
        ),
        "analysis_state": build_analysis_state_projection(state),
        "recent_agent_events": [_project_message(message) for message in recent_messages],
        "omitted_earlier_events": max(0, len(non_system_messages) - len(recent_messages)),
        "recovery_guidance": (
            "The structured analysis state is authoritative. Earlier capability facts omitted "
            "from recent_agent_events must be fetched again through the bounded capabilities."
        ),
    }
