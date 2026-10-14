"""Agent-side bindings retain the target and distinguish common intent errors."""

from types import SimpleNamespace

import pytest

from redteam.core.contracts.target import AssetTarget, EndpointTarget, HostTarget, UrlTarget
from redteam.core.contracts.tool import ToolDescriptor
from redteam.core.enums import ToolHealthStatus
from redteam.core.ids import AgentId, AssetId, ToolId
from redteam.orchestration.agents.base import PlanStep
from redteam.orchestration.candidates.catalog import bound_operations
from redteam.orchestration.candidates.compiler import CandidateCompiler
from redteam.orchestration.candidates.models import CandidateDraft
from redteam.orchestration.engine import (
    DecisionRuleError,
    _rejection_feedback,
    asset_argument_bindings,
    target_aliases,
    validate_argument_target,
)
from tests.integration import _engine_llm_harness as h


def tool(*fields, operation="service_discovery", capability="recon"):
    return ToolDescriptor(
        tool_id=ToolId(f"{capability}.tool"),
        server_id="synthetic",
        tool_name="tool",
        input_schema={
            "type": "object",
            "properties": {field: {} for field in fields},
            "required": list(fields),
            "additionalProperties": False,
        },
        capability_tags=[capability],
        operations=[operation],
        health_status=ToolHealthStatus.HEALTHY,
    )


def compile_target(target, descriptor, *, arguments=None, catalog=None):
    step = PlanStep(
        step_id="probe",
        capability=descriptor.capability_tags[0],
        operation=descriptor.operations[0],
        tool_id=descriptor.tool_id,
        target=target,
        arguments=arguments or {},
        reason="synthetic check",
    )
    from redteam.attack_graph.identity import node_id_for_target

    draft = CandidateDraft(
        agent_id=AgentId("recon-agent"),
        plan_step=step,
        target_node_ids=[node_id_for_target(target)],
    )
    return CandidateCompiler(catalog or h.FakeCatalog([descriptor])).compile_drafts_with_rejections(
        [draft]
    )


@pytest.mark.parametrize(
    "target",
    [
        HostTarget(host="example.test:8443"),
        EndpointTarget(host="example.test", port=8443),
        HostTarget(host="[2001:db8::1]:8443"),
    ],
)
def test_pinned_target_binding_keeps_authority_without_separate_port(target):
    descriptor = tool("host", "target_ref")
    compiled, errors = compile_target(target, descriptor)
    assert not errors
    args = compiled[0].plan_step.arguments
    assert args["host"] == args["target_ref"]
    assert args["host"].endswith(":8443")
    validate_argument_target(target, args)


def test_pinned_target_binding_splits_host_only_when_schema_carries_port():
    target = HostTarget(host="example.test:8443")
    compiled, errors = compile_target(target, tool("host", "port", "target_ref"))
    assert not errors
    assert compiled[0].plan_step.arguments == {
        "host": "example.test",
        "port": 8443,
        "target_ref": "example.test:8443",
    }
    validate_argument_target(target, compiled[0].plan_step.arguments)


@pytest.mark.parametrize(
    "arguments",
    [
        {"target_ref": "example.test"},
        {"host": "example.test"},
        {"host": "example.test", "port": 443},
        {"url": "https://example.test/"},
        {"host": "other.test:8443"},
        {"port": "secret-invalid-port"},
    ],
)
def test_pinned_arguments_cannot_silently_widen_target(arguments):
    target = UrlTarget(url="https://example.test:8443/guide")
    deps = SimpleNamespace(engagement=None)
    assert "example.test" not in target_aliases(deps, target)
    with pytest.raises(DecisionRuleError) as rejected:
        validate_argument_target(target, arguments, target_aliases(deps, target))
    assert rejected.value.diagnostic.code == (
        "target_ref_source_mismatch" if "target_ref" in arguments else "unresolved_target"
    )
    assert "secret-invalid-port" not in rejected.value.diagnostic.feedback()


def test_opaque_target_ref_requires_exact_pinned_metadata_key():
    engagement = h.make_engagement("example.test:8443").model_copy(
        update={
            "metadata": {"target_refs": {"example.test": "bare", "example.test:8443": "pinned"}}
        }
    )
    target = HostTarget(host="example.test:8443")
    assert target_aliases(SimpleNamespace(engagement=engagement), target) == {"pinned"}


@pytest.mark.parametrize(
    "url", ["https://example.test/other", "https://example.test/guide?q=other"]
)
def test_page_read_cannot_substitute_another_resource(url):
    descriptor = tool("url", operation="page_read", capability="web")
    compiled, errors = compile_target(
        UrlTarget(url="https://example.test/guide"), descriptor, arguments={"url": url}
    )
    assert not compiled and errors[0].reason == "unresolved_target"


def test_page_read_requires_url_even_if_tool_metadata_is_unconstrained():
    compiled, errors = compile_target(
        HostTarget(host="example.test"), tool("url", operation="page_read", capability="web")
    )
    assert not compiled and errors[0].reason == "unresolved_target"


def test_search_does_not_convert_investigation_url_into_tool_input():
    descriptor = tool("query", operation="web_search", capability="web")
    descriptor.input_schema["properties"]["url"] = {}
    compiled, errors = compile_target(
        UrlTarget(url="https://example.test/guide"), descriptor, arguments={"query": "reference"}
    )
    assert not errors and compiled[0].plan_step.arguments == {"query": "reference"}
    assert compiled[0].plan_step.target.url == "https://example.test/guide"


def test_asset_and_apk_policy_refs_are_distinct():
    configured = h.make_engagement().model_copy(
        update={
            "scope": h.make_engagement().scope.model_copy(update={"allowed_assets": ["package"]}),
            "metadata": {"asset_ref": "workspace", "apk_ref": "apk-policy"},
        }
    )
    target = AssetTarget(asset_id=AssetId("package"))
    bindings = asset_argument_bindings(SimpleNamespace(engagement=configured), target)
    validate_argument_target(
        target, {"asset_ref": "workspace", "apk_ref": "apk-policy"}, asset_bindings=bindings
    )
    with pytest.raises(DecisionRuleError):
        validate_argument_target(target, {"apk_ref": "workspace"}, asset_bindings=bindings)


def test_catalog_menu_hides_supported_but_unbound_operations():
    descriptor = tool("host")

    class UnboundCatalog(h.FakeCatalog):
        def list_tools_for_action(self, capability, operation):
            return []

    catalog = UnboundCatalog([descriptor])
    assert bound_operations(catalog, descriptor, "recon") == []
    compiled, errors = compile_target(HostTarget(host="example.test"), descriptor, catalog=catalog)
    assert not compiled and errors[0].reason == "no_tool_for_operation"
    assert "no_tool_for_operation" in _rejection_feedback(errors[0].reason, errors[0].field)


def test_catalog_descriptor_change_is_rejected_before_execution_selection():
    descriptor = tool("host")

    class ChangedCatalog(h.FakeCatalog):
        def get(self, tool_id):
            return descriptor.model_copy(update={"server_id": "changed"})

    compiled, errors = compile_target(
        HostTarget(host="example.test"), descriptor, catalog=ChangedCatalog([descriptor])
    )
    assert not compiled and errors[0].reason == "tool_catalog_inconsistent"


def test_supported_operation_and_tool_method_remain_separate():
    descriptor = tool("host")
    descriptor.tool_name = "vendor_inventory"
    compiled, errors = compile_target(HostTarget(host="example.test"), descriptor)
    assert not errors and compiled[0].plan_step.operation == "service_discovery"
    assert compiled[0].tool.tool_name == "vendor_inventory"
    step = compiled[0].plan_step.model_copy(update={"operation": "unknown_common_intent"})
    draft = compiled[0].draft.model_copy(update={"plan_step": step})
    accepted, rejected = CandidateCompiler(
        h.FakeCatalog([descriptor])
    ).compile_drafts_with_rejections([draft])
    assert not accepted and rejected[0].reason == "no_tool_for_operation"


@pytest.mark.parametrize(
    "reason",
    [
        "operation_unsupported",
        "no_tool_for_operation",
        "target_type_mismatch",
        "missing_required_argument",
    ],
)
def test_feedback_keeps_contract_diagnostic_reason(reason):
    feedback = _rejection_feedback(reason, "operation" if "operation" in reason else "arguments")
    assert ("invalid_arguments" if reason == "missing_required_argument" else reason) in feedback
