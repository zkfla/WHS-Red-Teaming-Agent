"""TASK-008 shell provider wiring: settings, loader, assembly, CLI, approval.

Owned by the Capability team (settings/loader/assembly/manifest) with Control
regression coverage owned jointly with the Control contract (no Control code
changes here: these tests pin the existing engine behavior for MEDIUM shell
risk). No Docker, no network: the workroom manager is constructed but never
executed; gateway discovery only.
"""

from __future__ import annotations

import argparse
import json

import pytest

from redteam.app.cli.commands import tools as tools_command
from redteam.capability.bootstrap import build_mcp_tool_gateway
from redteam.capability.settings import (
    GATED_NETWORK_POLICY_ENV_VAR,
    SHELL_POLICY_ENV_VAR,
    MCPConfigurationError,
    gated_network_policy_path_from_environment,
    shell_policy_path_from_environment,
)
from redteam.capability.shell_assembly import (
    build_gated_provider_config,
    shell_server_output_limits,
)
from redteam.capability.shell_manifest import (
    JOB_START_TOOL_NAME,
    RUN_TOOL_NAME,
    SHELL_SERVER_ID,
    build_shell_descriptors,
    validate_shell_descriptors,
)
from redteam.capability.shell_policy import load_shell_workroom_policy
from redteam.capability.shell_runtime import ShellRuntime, build_shell_runtime
from redteam.control.policy import evaluate_policy
from redteam.control.service import DefaultControlPlane
from redteam.core.contracts.action import ActionProposal
from redteam.core.contracts.engagement import (
    ApprovalPolicy,
    CommandApprovalRule,
    Engagement,
    EngagementScope,
    ExecutionBudget,
    RateLimitPolicy,
)
from redteam.core.contracts.policy import ControlContext
from redteam.core.contracts.target import HostTarget
from redteam.core.enums import PolicyVerdict, RiskLevel
from redteam.core.ids import (
    ActionId,
    AgentId,
    AgentInstanceId,
    EngagementId,
    InvocationId,
    RunId,
    ToolId,
    new_id,
)

_DIGEST = "ab" * 32


def _policy_file(tmp_path, **overrides):
    payload = {
        "image": f"example.com/workroom@sha256:{_DIGEST}",
    }
    payload.update(overrides)
    path = tmp_path / "shell-policy.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


def _shell_descriptor(name=RUN_TOOL_NAME):
    return {d.tool_name: d for d in build_shell_descriptors()}[name]


# --- settings source ------------------------------------------------------


def test_shell_policy_path_unset_means_disabled(monkeypatch):
    monkeypatch.delenv(SHELL_POLICY_ENV_VAR, raising=False)
    assert shell_policy_path_from_environment() is None


def test_shell_policy_path_blank_means_disabled(monkeypatch):
    monkeypatch.setenv(SHELL_POLICY_ENV_VAR, "   ")
    assert shell_policy_path_from_environment() is None


def test_shell_policy_path_returns_stripped_path(monkeypatch, tmp_path):
    target = _policy_file(tmp_path)
    monkeypatch.setenv(SHELL_POLICY_ENV_VAR, f"  {target}  ")
    assert shell_policy_path_from_environment() == target


def test_gated_network_policy_path_unset_means_closed(monkeypatch):
    monkeypatch.delenv(GATED_NETWORK_POLICY_ENV_VAR, raising=False)
    assert gated_network_policy_path_from_environment() is None


def test_gated_network_policy_path_blank_means_closed(monkeypatch):
    monkeypatch.setenv(GATED_NETWORK_POLICY_ENV_VAR, "   ")
    assert gated_network_policy_path_from_environment() is None


def test_gated_network_policy_path_returns_stripped_path(monkeypatch, tmp_path):
    target = str(tmp_path / "gated-policy.json")
    monkeypatch.setenv(GATED_NETWORK_POLICY_ENV_VAR, f"  {target}  ")
    assert gated_network_policy_path_from_environment() == target


def test_gated_network_policy_path_reads_explicit_source_mapping(tmp_path):
    target = str(tmp_path / "gated-policy.json")
    assert (
        gated_network_policy_path_from_environment(
            source={GATED_NETWORK_POLICY_ENV_VAR: target}
        )
        == target
    )
    assert gated_network_policy_path_from_environment(source={}) is None


def test_shell_policy_path_reads_explicit_source_mapping(tmp_path):
    target = _policy_file(tmp_path)
    assert shell_policy_path_from_environment(source={SHELL_POLICY_ENV_VAR: target}) == target
    assert shell_policy_path_from_environment(source={}) is None


# --- policy loader ----------------------------------------------------------


def test_loader_reads_valid_policy(tmp_path):
    policy = load_shell_workroom_policy(_policy_file(tmp_path))
    assert policy.max_jobs >= 1


def test_loader_rejects_missing_file(tmp_path):
    with pytest.raises(ValueError, match="unreadable"):
        load_shell_workroom_policy(str(tmp_path / "absent.json"))


def test_loader_rejects_bad_json(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{nope", encoding="utf-8")
    with pytest.raises(ValueError, match="not valid JSON"):
        load_shell_workroom_policy(str(path))


def test_loader_rejects_non_object_json(tmp_path):
    path = tmp_path / "list.json"
    path.write_text("[1, 2]", encoding="utf-8")
    with pytest.raises(ValueError, match="must be a JSON object"):
        load_shell_workroom_policy(str(path))


def test_loader_rejects_unpinned_image(tmp_path):
    with pytest.raises(ValueError, match="not pinned"):
        load_shell_workroom_policy(_policy_file(tmp_path, image="python:3.11-slim"))


def test_loader_schema_violation_hides_input_values(tmp_path):
    path = tmp_path / "evil.json"
    path.write_text(
        json.dumps({"image": "x", "max_jobs": "NOTANINT-SECRET", "bogus": "S3CR3T"}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError) as caught:
        load_shell_workroom_policy(str(path))
    message = str(caught.value)
    assert "NOTANINT-SECRET" not in message
    assert "S3CR3T" not in message
    assert "max_jobs" in message


def test_shell_runtime_fields_use_narrow_port_types():
    from redteam.core.contracts.tool import ToolDescriptor
    from redteam.core.ports.program_discovery import ShellProgramDiscovery

    fields = ShellRuntime.__dataclass_fields__
    assert fields["program_source"].type == "ShellProgramDiscovery"
    assert fields["descriptors"].type == "list[ToolDescriptor]"
    assert fields["concurrency"].type == "dict[str, int]"
    assert fields["output_limits"].type == "dict[str, int]"
    assert ShellProgramDiscovery is not None and ToolDescriptor is not None


# --- ShellRuntime assembly --------------------------------------------------


def test_build_shell_runtime_none_means_disabled():
    assert build_shell_runtime(None) is None


def test_build_shell_runtime_binds_same_program_source(tmp_path):
    runtime = build_shell_runtime(_policy_file(tmp_path))
    assert isinstance(runtime, ShellRuntime)
    assert runtime.program_source is runtime.adapter.program_source
    validate_shell_descriptors(runtime.descriptors, runtime.policy)
    assert runtime.concurrency == {SHELL_SERVER_ID: runtime.policy.max_jobs}
    assert runtime.output_limits == shell_server_output_limits(runtime.policy)
    assert {d.tool_name for d in runtime.descriptors} == {
        RUN_TOOL_NAME, JOB_START_TOOL_NAME, "shell_job_status", "shell_job_cancel",
    }


def _gated_policy_file(tmp_path, **overrides):
    from redteam.capability.gated_network_policy import GatedNetworkPolicy, PinnedRegistry
    from redteam.core.contracts.network import (
        NetworkBudget,
        NetworkCapacity,
        NetworkPurpose,
        WindowCaps,
    )

    cap = NetworkCapacity(max_flow_init_deliveries=10, max_output_bytes=100000)
    zero = NetworkCapacity(max_flow_init_deliveries=0, max_output_bytes=0)
    policy = GatedNetworkPolicy(
        sidecar_image="sha256:" + _DIGEST,
        budget=NetworkBudget(
            global_capacity=cap, target_capacity=cap, install_capacity=zero
        ),
        default_target_caps=WindowCaps(
            max_flow_init_deliveries=4, max_output_bytes=4096,
            delivery_rate_per_second=None, duration_seconds=60.0,
            purpose=NetworkPurpose.TARGET,
        ),
        default_install_caps=WindowCaps(
            max_flow_init_deliveries=2, max_output_bytes=2048,
            delivery_rate_per_second=None, duration_seconds=60.0,
            purpose=NetworkPurpose.INSTALL,
        ),
        registry=PinnedRegistry(targets={"t": ("10.0.0.9",)}, mirrors={}),
    )
    payload = policy.model_dump(mode="json")
    payload.update(overrides)
    path = tmp_path / "gated-policy.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


def test_build_gated_provider_config_none_means_closed():
    assert build_gated_provider_config(None) is None


def test_build_gated_provider_config_reads_valid_policy(tmp_path):
    from redteam.capability.gated_activation import GatedProviderConfig
    from redteam.core.contracts.network import NetworkPurpose

    config = build_gated_provider_config(_gated_policy_file(tmp_path))
    assert isinstance(config, GatedProviderConfig)
    assert config.network_policy.default_target_caps.purpose is NetworkPurpose.TARGET


def test_build_gated_provider_config_invalid_policy_is_config_error(tmp_path):
    with pytest.raises(MCPConfigurationError):
        build_gated_provider_config(str(tmp_path / "absent.json"))
    bad = tmp_path / "bad.json"
    bad.write_text("{nope", encoding="utf-8")
    with pytest.raises(MCPConfigurationError, match="not valid JSON"):
        build_gated_provider_config(str(bad))
    lst = tmp_path / "list.json"
    lst.write_text("[1, 2]", encoding="utf-8")
    with pytest.raises(MCPConfigurationError, match="must be a JSON object"):
        build_gated_provider_config(str(lst))


def test_build_gated_provider_config_hides_input_values(tmp_path):
    path = tmp_path / "evil.json"
    path.write_text(
        json.dumps({"budget": "S3CR3T-BUDGET", "bogus": "S3CR3T"}),
        encoding="utf-8",
    )
    with pytest.raises(MCPConfigurationError) as caught:
        build_gated_provider_config(str(path))
    message = str(caught.value)
    assert "S3CR3T-BUDGET" not in message
    assert "S3CR3T" not in message
    assert "budget" in message


def test_build_shell_runtime_invalid_policy_is_config_error(tmp_path):
    with pytest.raises(MCPConfigurationError):
        build_shell_runtime(str(tmp_path / "absent.json"))


def test_build_shell_runtime_config_error_hides_input_values(tmp_path):
    path = tmp_path / "evil.json"
    path.write_text(
        json.dumps({"image": "x", "max_jobs": "NOTANINT-SECRET", "bogus": "S3CR3T"}),
        encoding="utf-8",
    )
    with pytest.raises(MCPConfigurationError) as caught:
        build_shell_runtime(str(path))
    message = str(caught.value)
    assert "NOTANINT-SECRET" not in message
    assert "S3CR3T" not in message
    assert "max_jobs" in message


# --- CLI tools gateway ------------------------------------------------------


async def test_shell_only_catalog_lists_shell_tools_without_example(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("REDTEAM_MCP_SERVERS", raising=False)
    monkeypatch.setenv(SHELL_POLICY_ENV_VAR, _policy_file(tmp_path))
    _gateway, descriptors = await tools_command._build_gateway()
    ids = {str(d.tool_id) for d in descriptors}
    assert "analysis.shell_run" in ids
    assert "analysis.shell_job_start" in ids
    assert not any("example" in item for item in ids)
    assert descriptors[0].server_id in {SHELL_SERVER_ID}


async def test_shell_tools_list_reports_medium_risk(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("REDTEAM_MCP_SERVERS", raising=False)
    monkeypatch.setenv(SHELL_POLICY_ENV_VAR, _policy_file(tmp_path))
    exit_code = await tools_command.run_list(argparse.Namespace(json=True))
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    by_name = {item["tool_name"]: item for item in payload}
    assert by_name["shell_run"]["risk_level"] == RiskLevel.MEDIUM.value
    assert by_name["shell_job_start"]["risk_level"] == RiskLevel.MEDIUM.value


async def test_shell_gateway_matches_build_application_wiring(monkeypatch, tmp_path):
    """tools and run resolve the same provider: identical tool ids and risk.

    Guards the tools/run parity the module docstring promises.
    """
    from redteam.capability.settings import mcp_server_ids_from_environment

    monkeypatch.delenv("REDTEAM_MCP_SERVERS", raising=False)
    monkeypatch.setenv(SHELL_POLICY_ENV_VAR, _policy_file(tmp_path))
    runtime = build_shell_runtime(shell_policy_path_from_environment())
    assert runtime is not None
    gateway = await build_mcp_tool_gateway(
        mcp_server_ids_from_environment(),
        builtin_adapters=[runtime.adapter],
        builtin_concurrency=dict(runtime.concurrency),
        builtin_output_limits=dict(runtime.output_limits),
    )
    by_name = {d.tool_name: d for d in gateway.all()}
    assert by_name[RUN_TOOL_NAME].risk_level == RiskLevel.MEDIUM
    assert by_name[JOB_START_TOOL_NAME].risk_level == RiskLevel.MEDIUM












async def test_tools_gateway_lists_four_shell_tools_when_gated_absent(monkeypatch, tmp_path):
    """Offline discovery without a gated policy shows the shell manifest only."""
    monkeypatch.delenv("REDTEAM_MCP_SERVERS", raising=False)
    monkeypatch.setenv(SHELL_POLICY_ENV_VAR, _policy_file(tmp_path))
    monkeypatch.delenv(GATED_NETWORK_POLICY_ENV_VAR, raising=False)
    _gateway, descriptors = await tools_command._build_gateway()
    assert {str(d.tool_id) for d in descriptors} == {
        "analysis.shell_run",
        "analysis.shell_job_start",
        "analysis.shell_job_status",
        "analysis.shell_job_cancel",
    }


async def test_tools_gateway_lists_configured_manifest_with_gated_policy(
    monkeypatch, tmp_path
):
    """Offline discovery advertises the configured manifest: 4 shell + 3 gated.

    Display/catalog metadata only: no activation, no window, no ledger file,
    no Docker execution. Gated rows carry the discovered shell-server health
    and keep their manifest operations; nothing here makes a gated tool
    invocable outside run/resume.
    """
    from redteam.capability.shell_workroom import ShellWorkroomManager

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("REDTEAM_MCP_SERVERS", raising=False)
    monkeypatch.setenv(SHELL_POLICY_ENV_VAR, _policy_file(tmp_path))
    monkeypatch.setenv(GATED_NETWORK_POLICY_ENV_VAR, _gated_policy_file(tmp_path))

    async def _no_docker(*args, **kwargs):
        raise AssertionError("offline discovery must not execute")

    monkeypatch.setattr(ShellWorkroomManager, "exec_script", _no_docker)
    _gateway, descriptors = await tools_command._build_gateway()
    by_id = {str(d.tool_id): d for d in descriptors}
    assert set(by_id) == {
        "analysis.shell_run",
        "analysis.shell_job_start",
        "analysis.shell_job_status",
        "analysis.shell_job_cancel",
        "analysis.shell_target_run",
        "analysis.shell_target_job_start",
        "analysis.shell_install_run",
    }
    gated = [
        by_id["analysis.shell_target_run"],
        by_id["analysis.shell_target_job_start"],
        by_id["analysis.shell_install_run"],
    ]
    assert all(d.operations for d in gated)
    shell_health = {
        d.health_status for d in descriptors if d.tool_name in {"shell_run", "shell_job_start"}
    }
    assert len(shell_health) == 1
    assert all(d.health_status in shell_health for d in gated)
    assert list(tmp_path.rglob("*.sqlite3")) == []
    assert list(tmp_path.rglob("*network_grants*")) == []


async def test_tools_gateway_rejects_gated_policy_without_shell(monkeypatch, tmp_path):
    monkeypatch.delenv("REDTEAM_MCP_SERVERS", raising=False)
    monkeypatch.delenv(SHELL_POLICY_ENV_VAR, raising=False)
    monkeypatch.setenv(GATED_NETWORK_POLICY_ENV_VAR, _gated_policy_file(tmp_path))
    with pytest.raises(MCPConfigurationError, match="requires the shell provider"):
        await tools_command._build_gateway()


async def test_tools_gateway_rejects_invalid_gated_policy(monkeypatch, tmp_path):
    monkeypatch.delenv("REDTEAM_MCP_SERVERS", raising=False)
    monkeypatch.setenv(SHELL_POLICY_ENV_VAR, _policy_file(tmp_path))
    bad = tmp_path / "gated-bad.json"
    bad.write_text("{nope", encoding="utf-8")
    monkeypatch.setenv(GATED_NETWORK_POLICY_ENV_VAR, str(bad))
    with pytest.raises(MCPConfigurationError, match="not valid JSON"):
        await tools_command._build_gateway()


# --- Control regressions (no Control changes; pin existing behavior) ----------


def _proposal(**updates):
    from datetime import datetime, timezone

    values = dict(
        action_id=ActionId("action_shell_008"),
        run_id=RunId("run_shell_008"),
        engagement_id=EngagementId("eng_shell_008"),
        agent_id=AgentId("agent_shell_008"),
        capability="analysis",
        operation="shell_run",
        target=HostTarget(host="good.local"),
        arguments={"language": "bash", "script": "echo hi"},
        reason="task-008 regression",
        created_at=datetime.now(timezone.utc),
    )
    values.update(updates)
    return ActionProposal(**values)


def _engagement(approval):
    from datetime import datetime, timezone

    return Engagement(
        engagement_id=EngagementId("eng_shell_008"),
        name="shell-008",
        scope=EngagementScope(allowed_hosts=["good.local"]),
        execution_budget=ExecutionBudget(max_actions=4),
        rate_limit_policy=RateLimitPolicy(max_actions_per_minute=30),
        approval_policy=approval,
        created_at=datetime.now(timezone.utc),
    )


def test_explicit_command_rule_without_min_risk_still_fires():
    """Regression (a): operator rules with minRisk None fire on MEDIUM scripts.

    The default destructive baseline (minimum_risk HIGH) stays silent below
    HIGH -- this test pins that baseline-HIGH-limited meaning too.
    """
    descriptor = _shell_descriptor()
    firing = CommandApprovalRule(name="op-rule", patterns=["exfil"], minimum_risk=None)
    verdict, _, matched = evaluate_policy(
        _proposal(arguments={"language": "bash", "script": "run exfil now"}),
        ApprovalPolicy(command_approval_rules=[firing]),
        risk=RiskLevel.MEDIUM,
        selected=descriptor,
        has_selected_tool=True,
    )
    assert verdict == PolicyVerdict.REQUIRE_APPROVAL
    assert "command:op-rule" in matched


def test_explicit_command_rule_with_medium_min_risk_still_fires():
    descriptor = _shell_descriptor()
    firing = CommandApprovalRule(name="op-med", patterns=["exfil"], minimum_risk=RiskLevel.MEDIUM)
    verdict, _, matched = evaluate_policy(
        _proposal(arguments={"language": "bash", "script": "run exfil now"}),
        ApprovalPolicy(command_approval_rules=[firing]),
        risk=RiskLevel.MEDIUM,
        selected=descriptor,
        has_selected_tool=True,
    )
    assert verdict == PolicyVerdict.REQUIRE_APPROVAL
    assert "command:op-med" in matched


def test_default_high_baseline_rule_does_not_fire_on_medium():
    """The shipped destructive baseline is HIGH-limited: MEDIUM shell text
    with a baseline pattern (rm -rf) does not match. No new default list,
    no global min_risk change -- pinned here.
    """
    from redteam.control.policy import default_command_approval_rules

    descriptor = _shell_descriptor()
    assert any(rule.minimum_risk == RiskLevel.HIGH for rule in default_command_approval_rules())
    verdict, _, matched = evaluate_policy(
        _proposal(arguments={"language": "bash", "script": "rm -rf /tmp/scratch"}),
        ApprovalPolicy(),
        risk=RiskLevel.MEDIUM,
        selected=descriptor,
        has_selected_tool=True,
    )
    assert verdict == PolicyVerdict.ALLOW
    assert matched == []


def test_clean_default_script_is_allow():
    """Regression (b, policy level): ambiguity alone never forces approval."""
    descriptor = _shell_descriptor()
    verdict, _, matched = evaluate_policy(
        _proposal(),
        ApprovalPolicy(),
        risk=RiskLevel.MEDIUM,
        selected=descriptor,
        has_selected_tool=True,
    )
    assert verdict == PolicyVerdict.ALLOW
    assert matched == []


async def test_clean_default_script_allow_charges_metering():
    """Regression (b, plane level): ALLOW consumes one budget/rate action."""
    from redteam.capability.gateway import MCPToolGateway
    from redteam.capability.registry import MCPToolRegistry

    descriptor = _shell_descriptor()
    registry = MCPToolRegistry()
    registry.replace_server_tools(SHELL_SERVER_ID, build_shell_descriptors())
    gateway = MCPToolGateway(registry, {}, {})
    engagement = _engagement(ApprovalPolicy())
    before = engagement.execution_budget.max_actions
    plane = DefaultControlPlane(
        {engagement.engagement_id: engagement},
        outbound_descriptor_resolver=gateway.get,
    )
    decision = await plane.evaluate(
        _proposal(),
        ControlContext(
            engagement_id=engagement.engagement_id,
            selected_tool_id=descriptor.tool_id,
        ),
    )
    assert decision.verdict == PolicyVerdict.ALLOW
    assert decision.approval_id is None
    assert decision.budget_remaining == before - 1


async def test_explicit_namespace_require_approval_holds_shell():
    """Regression (c): operator require_approval_for on the namespace holds."""
    from redteam.capability.gateway import MCPToolGateway
    from redteam.capability.registry import MCPToolRegistry

    descriptor = _shell_descriptor()
    registry = MCPToolRegistry()
    registry.replace_server_tools(SHELL_SERVER_ID, build_shell_descriptors())
    gateway = MCPToolGateway(registry, {}, {})
    engagement = _engagement(ApprovalPolicy(require_approval_for=["analysis"]))
    plane = DefaultControlPlane(
        {engagement.engagement_id: engagement},
        outbound_descriptor_resolver=gateway.get,
    )
    decision = await plane.evaluate(
        _proposal(),
        ControlContext(
            engagement_id=engagement.engagement_id,
            selected_tool_id=descriptor.tool_id,
        ),
    )
    assert decision.verdict == PolicyVerdict.REQUIRE_APPROVAL
    assert decision.approval_id is not None


async def test_explicit_low_threshold_holds_medium_shell():
    """Regression (d): operator risk_threshold LOW holds MEDIUM shell work."""
    from redteam.capability.gateway import MCPToolGateway
    from redteam.capability.registry import MCPToolRegistry

    descriptor = _shell_descriptor()
    registry = MCPToolRegistry()
    registry.replace_server_tools(SHELL_SERVER_ID, build_shell_descriptors())
    gateway = MCPToolGateway(registry, {}, {})
    engagement = _engagement(ApprovalPolicy(risk_threshold=RiskLevel.LOW))
    plane = DefaultControlPlane(
        {engagement.engagement_id: engagement},
        outbound_descriptor_resolver=gateway.get,
    )
    decision = await plane.evaluate(
        _proposal(),
        ControlContext(
            engagement_id=engagement.engagement_id,
            selected_tool_id=descriptor.tool_id,
        ),
    )
    assert decision.verdict == PolicyVerdict.REQUIRE_APPROVAL
    assert decision.approval_id is not None


async def test_unresolved_risk_still_escalates():
    """Regression (e): a broken risk source escalates to human, never ALLOW."""

    class _BrokenResolver:
        def resolve(self, capability, operation):
            raise RuntimeError("risk source down")
    engagement = _engagement(ApprovalPolicy())
    plane = DefaultControlPlane(
        {engagement.engagement_id: engagement},
        risk_resolver=_BrokenResolver(),
    )
    proposal = _proposal()
    decision = await plane.evaluate(
        proposal,
        ControlContext(engagement_id=engagement.engagement_id),
    )
    assert decision.verdict == PolicyVerdict.REQUIRE_APPROVAL
    assert "risk:unresolved" in decision.matched_rules


class _BinderFakeWorkroom:
    """Minimal workroom double: records executions, never touches Docker."""

    def __init__(self):
        self.exec_calls = []

    async def exec_script(
        self,
        key,
        language,
        script,
        timeout_seconds=None,
        exec_tag=None,
        *,
        invocation_id=None,
        input_digest=None,
        retain_record=True,
        extra_env=None,
    ):
        from redteam.capability.shell_workroom import ExecResult

        self.exec_calls.append((key, language, script, exec_tag, invocation_id, input_digest))
        return ExecResult(
            exit_code=0,
            stdout="proof:" + script[:16],
            stderr="",
            timed_out=False,
            truncated=False,
            duration_ms=1,
            workroom_key=key,
            container_name="fake-container",
        )

    async def probe(self):
        return True


async def test_shell_executes_through_generic_gateway_with_fresh_instance(tmp_path):
    """CLI-run proof: fresh run identity executes through gateway + binder.

    Covers the production gap where a shell-enabled run without instance_id
    could never execute: with a fresh (run_id, instance_id) pair the generic
    gateway -> bind_context -> adapter path runs a local script and the
    output carries the trusted derived workroom key.
    """
    from datetime import datetime, timezone

    from redteam.capability.bootstrap import build_mcp_tool_gateway
    from redteam.capability.shell_adapter import BuiltinShellAdapter
    from redteam.capability.shell_assembly import shell_server_output_limits
    from redteam.capability.shell_workroom import workroom_key_for
    from redteam.core.contracts.tool import ExecutionContext, ToolInvocation
    from redteam.core.tool_binding import SELECTED_TOOL

    policy = load_shell_workroom_policy(_policy_file(tmp_path))
    fake = _BinderFakeWorkroom()
    adapter = BuiltinShellAdapter(policy, workroom=fake)
    gateway = await build_mcp_tool_gateway(
        [],
        builtin_adapters=[adapter],
        builtin_concurrency={adapter.server_id: policy.max_jobs},
        builtin_output_limits=shell_server_output_limits(policy),
    )
    run_id = RunId(new_id("run"))
    instance_id = AgentInstanceId(new_id("instance"))
    invocation = ToolInvocation(
        invocation_id=InvocationId("inv-shell-proof"),
        action_id=ActionId("action-shell-proof"),
        run_id=run_id,
        tool_id=ToolId("analysis.shell_run"),
        binding_mode=SELECTED_TOOL,
        arguments={"language": "bash", "script": "echo hi"},
        execution_context=ExecutionContext(
            engagement_id=EngagementId("eng_shell_008"),
            run_id=run_id,
            instance_id=instance_id,
        ),
        created_at=datetime.now(timezone.utc),
    )
    result = await gateway.invoke(invocation)
    assert result.success is True
    assert result.output["workroom_key"] == workroom_key_for(str(run_id), str(instance_id))
    assert len(fake.exec_calls) == 1
    _key, _language, _script, exec_tag, invocation_id, input_digest = fake.exec_calls[0]
    assert exec_tag == "inv-shell-proof"
    assert invocation_id == "inv-shell-proof"
    assert input_digest




async def test_shell_binder_rejects_foreign_key_without_executing(tmp_path):
    """Cross-instance protection: a mismatched key never reaches execution."""
    from datetime import datetime, timezone

    from redteam.capability.bootstrap import build_mcp_tool_gateway
    from redteam.capability.shell_adapter import BuiltinShellAdapter
    from redteam.capability.shell_assembly import shell_server_output_limits
    from redteam.core.contracts.tool import ExecutionContext, ToolInvocation
    from redteam.core.tool_binding import SELECTED_TOOL

    policy = load_shell_workroom_policy(_policy_file(tmp_path))
    fake = _BinderFakeWorkroom()
    adapter = BuiltinShellAdapter(policy, workroom=fake)
    gateway = await build_mcp_tool_gateway(
        [],
        builtin_adapters=[adapter],
        builtin_concurrency={adapter.server_id: policy.max_jobs},
        builtin_output_limits=shell_server_output_limits(policy),
    )
    run_id = RunId(new_id("run"))
    invocation = ToolInvocation(
        invocation_id=InvocationId("inv-shell-foreign"),
        action_id=ActionId("action-shell-foreign"),
        run_id=run_id,
        tool_id=ToolId("analysis.shell_run"),
        binding_mode=SELECTED_TOOL,
        arguments={"language": "bash", "script": "echo hi", "workroom_key": "forged-key"},
        execution_context=ExecutionContext(
            engagement_id=EngagementId("eng_shell_008"),
            run_id=run_id,
            instance_id=AgentInstanceId(new_id("instance")),
        ),
        created_at=datetime.now(timezone.utc),
    )
    result = await gateway.invoke(invocation)
    assert result.success is False
    assert result.error is not None and result.error.code == "shell_context_mismatch"
    assert fake.exec_calls == []


async def test_shell_binder_requires_instance_identity(tmp_path):
    """Missing instance_id (the run_engagement gap) fails closed, never executes."""
    from datetime import datetime, timezone

    from redteam.capability.bootstrap import build_mcp_tool_gateway
    from redteam.capability.shell_adapter import BuiltinShellAdapter
    from redteam.capability.shell_assembly import shell_server_output_limits
    from redteam.core.contracts.tool import ExecutionContext, ToolInvocation
    from redteam.core.tool_binding import SELECTED_TOOL

    policy = load_shell_workroom_policy(_policy_file(tmp_path))
    fake = _BinderFakeWorkroom()
    adapter = BuiltinShellAdapter(policy, workroom=fake)
    gateway = await build_mcp_tool_gateway(
        [],
        builtin_adapters=[adapter],
        builtin_concurrency={adapter.server_id: policy.max_jobs},
        builtin_output_limits=shell_server_output_limits(policy),
    )
    run_id = RunId(new_id("run"))
    invocation = ToolInvocation(
        invocation_id=InvocationId("inv-shell-noinst"),
        action_id=ActionId("action-shell-noinst"),
        run_id=run_id,
        tool_id=ToolId("analysis.shell_run"),
        binding_mode=SELECTED_TOOL,
        arguments={"language": "bash", "script": "echo hi"},
        execution_context=ExecutionContext(
            engagement_id=EngagementId("eng_shell_008"),
            run_id=run_id,
            instance_id=None,
        ),
        created_at=datetime.now(timezone.utc),
    )
    result = await gateway.invoke(invocation)
    assert result.success is False
    assert result.error is not None and result.error.code == "shell_context_mismatch"
    assert fake.exec_calls == []
