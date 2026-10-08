"""Regression checks for ownership and physical-tool evidence connections."""

from __future__ import annotations

import ast
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from redteam.core.contracts.action import ActionProposal
from redteam.core.contracts.evidence import EvidenceRecord
from redteam.core.contracts.target import HostTarget
from redteam.core.contracts.tool import ToolResult
from redteam.core.enums import EvidenceSourceType
from redteam.core.ids import (
    ActionId,
    AgentId,
    EngagementId,
    EvidenceId,
    InvocationId,
    RunId,
    ToolId,
)
from redteam.orchestration.agents.base import PlanStep
from redteam.orchestration.authentication import AuthenticationCheck, assess_authentication
from redteam.orchestration.nodes.verify import verify_node
from redteam.orchestration.observations import ResultMapperRegistry
from redteam.orchestration.observations.web_http import map_web_http_request
from redteam.orchestration.state import RuntimeState, initial_graph_state
from tests.fakes import FakeEvidenceStore, build_test_application

NOW = datetime.now(timezone.utc)
ARGS = {"target_ref": "approved-app", "path": "/resource", "method": "GET"}
OUTPUT: dict[str, Any] = {
    "target_ref": "approved-app",
    "session_ref": None,
    "status_code": 401,
    "body_bytes": 0,
    "body_sha256": "a" * 64,
    "redirect": False,
    "security_headers_present": {},
    "body_omitted": True,
}


def result(tool_id: str, *, status: int = 401) -> ToolResult:
    return ToolResult(
        invocation_id=InvocationId("inv_review"),
        action_id=ActionId("action_review"),
        tool_id=ToolId(tool_id),
        success=True,
        output={**OUTPUT, "status_code": status},
        started_at=NOW,
        finished_at=NOW,
    )


def proposal(*, session: bool = False) -> ActionProposal:
    return ActionProposal(
        action_id=ActionId("action_review"),
        run_id=RunId("run_review"),
        engagement_id=EngagementId("eng_review"),
        agent_id=AgentId("review-worker"),
        capability="web",
        operation="http_request",
        target=HostTarget(host="app.test"),
        arguments={**ARGS, **({"session_ref": "test-session"} if session else {})},
        reason="bounded observation",
        created_at=NOW,
    )


@pytest.mark.parametrize(
    "tool_id", ["web.http_request", "web.http-a.http_request", "web.http-b.fetch_response"]
)
def test_explicit_http_mapper_is_not_bound_to_a_tool_spelling(tool_id: str) -> None:
    registry = ResultMapperRegistry()
    registry.register(tool_id, map_web_http_request)
    mapped = registry.map_result_with_outcome(
        tool_id=ToolId(tool_id),
        target=HostTarget(host="app.test"),
        arguments=ARGS,
        result=result(tool_id),
    )
    observations = [item for node in mapped.mutation.nodes for item in node.observations]
    assert mapped.status == "mapped"
    assert any(item.get("kind") == "http_response" for item in observations)


@pytest.mark.parametrize("registered,valid_shape", [(True, True), (False, True), (True, False)])
@pytest.mark.parametrize("tool_id", ["web.http_request", "web.http-a.http_request"])
async def test_verify_uses_the_registered_validated_mapping(
    registered: bool,
    valid_shape: bool,
    tool_id: str,
) -> None:
    registry = ResultMapperRegistry()
    if registered:
        registry.register(tool_id, map_web_http_request)
    deps = replace(build_test_application(), result_mappers=registry)
    payload = result(tool_id)
    if not valid_shape:
        payload = payload.model_copy(update={"output": {"status_code": 401}})
    state = initial_graph_state(
        RuntimeState(run_id=RunId("run_review"), engagement_id=EngagementId("eng_review"))
    )
    state["action_proposal"] = proposal()
    state["tool_result"] = payload
    state["plan_step"] = PlanStep(
        step_id="bounded-read",
        tool_id=ToolId(tool_id),
        capability="web",
        operation="http_request",
        target=HostTarget(host="app.test"),
        assessment_kind="observation",
        reason="bounded observation",
    )
    await verify_node(state, deps=deps)
    records = await deps.evidence_store.list_for_run(RunId("run_review"))
    record = next(record for record in records if record.source_type == EvidenceSourceType.TOOL)
    assert ("http_observation" in record.metadata) is (registered and valid_shape)
    assert record.source_id == tool_id


def evidence(tool_id: str, *, baseline: bool, run: str = "run_review") -> EvidenceRecord:
    return EvidenceRecord(
        evidence_id=EvidenceId("ev_baseline" if baseline else "ev_current"),
        run_id=RunId(run),
        engagement_id=EngagementId("eng_review"),
        source_type=EvidenceSourceType.TOOL,
        source_id=tool_id,
        action_id=ActionId("action_baseline" if baseline else "action_review"),
        invocation_id=InvocationId("inv_baseline" if baseline else "inv_review"),
        content_type="application/json",
        digest="a" * 64,
        summary="HTTP observation",
        created_at=NOW,
        metadata={
            "success": True,
            "target_node_id": "host:app.test",
            "http_observation": {
                "kind": "http_response",
                **ARGS,
                "status_code": 401 if baseline else 200,
                "session_ref": None if baseline else "test-session",
            },
        },
    )


@pytest.mark.parametrize(
    "baseline_id,current_id",
    [
        ("web.http_request", "web.http_request"),
        ("web.http-a.http_request", "web.http-a.http_request"),
        ("web.http-a.http_request", "web.http-b.fetch_response"),
    ],
)
async def test_auth_assessment_uses_observation_contract_not_tool_names(
    baseline_id: str,
    current_id: str,
) -> None:
    store = FakeEvidenceStore()
    await store.append(evidence(baseline_id, baseline=True))
    assessment = await assess_authentication(
        AuthenticationCheck(
            baseline_evidence_id=EvidenceId("ev_baseline"), session_ref="test-session"
        ),
        proposal(session=True),
        result(current_id, status=200),
        evidence(current_id, baseline=False),
        store,
    )
    assert assessment.outcome == "session_access_observed"
    # Observing accepted access is not proof of a vulnerability.
    assert assessment.outcome != "access_control_violation"


@pytest.mark.parametrize("mismatch", ["run", "current_source", "current_status", "current_path"])
async def test_unrelated_auth_evidence_cannot_confirm_success(mismatch: str) -> None:
    store = FakeEvidenceStore()
    await store.append(
        evidence(
            "web.http_request",
            baseline=True,
            run="run_other" if mismatch == "run" else "run_review",
        )
    )
    current = evidence("web.http_request", baseline=False)
    if mismatch == "current_source":
        current.source_id = "web.unregistered.lookalike"
    elif mismatch == "current_status":
        current.metadata["http_observation"]["status_code"] = 200
    elif mismatch == "current_path":
        current.metadata["http_observation"]["path"] = "/other"
    assessment = await assess_authentication(
        AuthenticationCheck(
            baseline_evidence_id=EvidenceId("ev_baseline"), session_ref="test-session"
        ),
        proposal(session=True),
        result("web.http_request", status=403 if mismatch == "current_status" else 200),
        current,
        store,
    )
    assert assessment.hypothesis_status != "confirmed"


def test_evidence_and_reference_validation_are_leaf_dependencies() -> None:
    root = Path(__file__).resolve().parents[2] / "src/redteam"
    for path, forbidden in [
        (root / "evidence/service.py", "redteam.control"),
        (root / "orchestration/specialist_planning.py", "redteam.orchestration.engine"),
    ]:
        imports = [
            node.module
            for node in ast.walk(ast.parse(path.read_text()))
            if isinstance(node, ast.ImportFrom)
        ]
        assert not any(module and module.startswith(forbidden) for module in imports)


def test_source_layer_boundaries_do_not_depend_on_adapter_or_cli_implementations() -> None:
    root = Path(__file__).resolve().parents[2] / "src/redteam"
    rules = {
        "core": {
            "app",
            "capability",
            "control",
            "evidence",
            "llm",
            "orchestration",
            "attack_graph",
            "event_bus",
            "intake",
        },
        "orchestration": {"app", "capability", "control", "llm"},
        "capability": {"app", "orchestration", "control", "llm"},
        "control": {"app", "orchestration", "capability", "llm"},
        "evidence": {"app", "orchestration", "capability", "control", "llm"},
    }
    violations = []
    for area, forbidden in rules.items():
        for path in (root / area).rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text())):
                if not isinstance(node, (ast.ImportFrom, ast.Import)):
                    continue
                modules = (
                    [node.module or ""]
                    if isinstance(node, ast.ImportFrom)
                    else [alias.name for alias in node.names]
                )
                for module in modules:
                    parts = module.split(".")
                    if len(parts) > 1 and parts[0] == "redteam" and parts[1] in forbidden:
                        violations.append(f"{path.relative_to(root)}:{node.lineno}: {module}")
    assert not violations, "\n".join(violations)


def test_shared_helpers_preserve_existing_public_imports() -> None:
    from redteam.control.audit import redact_arguments as legacy_redactor
    from redteam.core.redaction import redact_arguments
    from redteam.orchestration.engine import principal_bindings as legacy_bindings
    from redteam.orchestration.knowledge_validation import principal_bindings

    assert legacy_redactor is redact_arguments
    assert legacy_bindings is principal_bindings
    payload: dict[str, Any] = {
        "nested": [{"session_token": "private", "note": "Authorization: Bearer private"}],
        "count": 2,
    }
    masked = redact_arguments(payload)
    assert "private" not in str(masked)
    assert payload["nested"][0]["session_token"] == "private"
    assert principal_bindings({}) == {"anonymous": set()}
