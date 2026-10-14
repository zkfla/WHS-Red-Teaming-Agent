"""Tool-owned target_ref source binding across compiler and admission."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from redteam.core.contracts.target import DomainTarget, EndpointTarget, UrlTarget
from redteam.core.contracts.tool import ToolDescriptor, ToolTargetBinding
from redteam.core.enums import ToolHealthStatus
from redteam.core.ids import AgentId, ToolId
from redteam.orchestration.agents.base import PlanStep
from redteam.orchestration.candidates.compiler import CandidateCompiler
from redteam.orchestration.candidates.models import CandidateDraft
from redteam.orchestration.engine import DecisionRuleError, validate_argument_target
from redteam.orchestration.knowledge_validation import InternalAssessment, InternalTarget
from redteam.orchestration.native_binding import resolve_native_target


def _tool(source: str = "mcp_ref") -> ToolDescriptor:
    return ToolDescriptor(
        tool_id=ToolId("recon.lookup"),
        server_id="synthetic",
        tool_name="lookup",
        input_schema={
            "type": "object",
            "properties": {
                "target_ref": {"type": "string"},
                "host": {"type": "string"},
                "port": {"type": "integer"},
            },
            "required": ["target_ref"],
            "additionalProperties": False,
        },
        operations=["service_discovery"],
        capability_tags=["recon"],
        health_status=ToolHealthStatus.HEALTHY,
        target_binding=ToolTargetBinding(target_ref_source=source),
    )


class _Catalog:
    def __init__(self, descriptor: ToolDescriptor):
        self.descriptor = descriptor

    def get(self, _tool_id):
        return self.descriptor

    def list_tools_for_action(self, _capability, _operation):
        return [self.descriptor]


def _compile(target, descriptor, arguments):
    from redteam.attack_graph.identity import node_id_for_target

    draft = CandidateDraft(
        agent_id=AgentId("recon-agent"),
        plan_step=PlanStep(
            step_id="lookup",
            capability="recon",
            operation="service_discovery",
            tool_id=descriptor.tool_id,
            target=target,
            arguments=arguments,
            reason="check selected authority",
        ),
        target_node_ids=[node_id_for_target(target)],
    )
    return CandidateCompiler(_Catalog(descriptor)).compile_drafts_with_rejections([draft])


def test_binding_schema_defaults_to_mcp_ref_and_preserves_legacy_dump():
    default = ToolTargetBinding()
    logical = ToolTargetBinding(target_ref_source="logical_host")

    assert default.target_ref_source == "mcp_ref"
    assert "target_ref_source" not in default.model_dump(mode="json")
    assert "target_ref_source" not in ToolTargetBinding(target_ref_source="mcp_ref").model_dump(
        mode="json"
    )
    assert logical.model_dump(mode="json")["target_ref_source"] == "logical_host"
    schema = ToolTargetBinding.model_json_schema()["properties"]["target_ref_source"]
    assert schema["default"] == "mcp_ref"
    assert schema["enum"] == ["mcp_ref", "logical_host"]
    assert "target_ref_source" not in _tool("mcp_ref").model_dump(mode="json")["target_binding"]
    assert _tool("logical_host").model_dump(mode="json")["target_binding"][
        "target_ref_source"
    ] == "logical_host"
    with pytest.raises(ValidationError):
        ToolTargetBinding.model_validate({"target_ref_source": "registry_alias"})


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        (DomainTarget(domain="t.test"), "t.test"),
        (EndpointTarget(host="t.test", port=8443), "t.test"),
        (UrlTarget(url="https://t.test:8443/path"), "t.test"),
    ],
)
def test_compiler_synthesizes_logical_host_over_caller_target_ref(target, expected):
    compiled, rejected = _compile(
        target,
        _tool("logical_host"),
        {"target_ref": "deployment-alias"},
    )

    assert not rejected
    assert compiled[0].draft.plan_step.arguments["target_ref"] == expected


def test_compiler_preserves_explicit_mcp_reference():
    compiled, rejected = _compile(
        DomainTarget(domain="t.test"),
        _tool("mcp_ref"),
        {"target_ref": "deployment-alias"},
    )

    assert not rejected
    assert compiled[0].draft.plan_step.arguments["target_ref"] == "deployment-alias"


def test_engine_validator_uses_only_declared_target_ref_form():
    target = DomainTarget(domain="t.test")
    logical = ToolTargetBinding(target_ref_source="logical_host")
    mcp = ToolTargetBinding(target_ref_source="mcp_ref")

    validate_argument_target(
        target,
        {"target_ref": "t.test"},
        {"deployment-alias", "t.test"},
        target_binding=logical,
    )
    with pytest.raises(DecisionRuleError, match="descriptor-declared source") as rejected:
        validate_argument_target(
            target,
            {"target_ref": "deployment-alias"},
            {"deployment-alias", "t.test"},
            target_binding=logical,
        )
    assert rejected.value.diagnostic.code == "target_ref_source_mismatch"
    assert "deployment-alias" not in rejected.value.diagnostic.feedback()
    validate_argument_target(
        target,
        {"target_ref": "deployment-alias"},
        {"deployment-alias", "t.test"},
        target_binding=mcp,
    )
    with pytest.raises(DecisionRuleError, match="descriptor-declared source"):
        validate_argument_target(
            target,
            {"target_ref": "t.test"},
            {"deployment-alias"},
            target_binding=mcp,
        )


def test_native_binding_reads_the_descriptor_source_without_name_inference():
    from redteam.attack_graph.identity import node_id_for_target

    target = DomainTarget(domain="t.test")
    node_id = str(node_id_for_target(target))
    material = InternalAssessment(
        targets={node_id: InternalTarget(node_id=node_id, execution_target=target)}
    )
    tool = _tool("logical_host")
    deps = SimpleNamespace(graph_store=None, engagement=None, operation_bindings=None)

    selected = asyncio.run(resolve_native_target(deps, tool, {"target_ref": "t.test"}, material))
    assert selected == target
    with pytest.raises(DecisionRuleError) as rejected:
        asyncio.run(
            resolve_native_target(deps, tool, {"target_ref": "deployment-alias"}, material)
        )
    assert rejected.value.diagnostic.code == "target_ref_source_mismatch"
