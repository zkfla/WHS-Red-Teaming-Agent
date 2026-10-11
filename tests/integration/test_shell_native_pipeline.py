"""Shell runs retain Control, Work and original-evidence execution contracts."""

from dataclasses import replace

import pytest

from redteam.attack_graph.service import AttackGraphService
from redteam.capability.bootstrap import build_mcp_tool_gateway
from redteam.capability.shell_adapter import BuiltinShellAdapter
from redteam.capability.shell_manifest import RUN_TOOL_NAME, SHELL_NAMESPACE
from redteam.capability.shell_policy import ShellWorkroomPolicy
from redteam.capability.shell_workroom import ExecResult, workroom_key_for
from redteam.control.service import DefaultControlPlane
from redteam.core.contracts.engagement import ApprovalPolicy
from redteam.core.enums import EvidenceSourceType, RiskLevel
from redteam.core.ids import AgentId, AgentInstanceId, RunId
from redteam.core.ports.llm import NativeCompletion, NativeToolCall
from redteam.orchestration.agents.base import AgentSpec
from redteam.orchestration.agents.definition import AgentDefinition, ContextProfile
from redteam.orchestration.agents.registry import AgentRegistry
from redteam.orchestration.graph import build_graph
from redteam.orchestration.native_binding import exposed_tool_name
from redteam.orchestration.seed import seed_from_engagement
from redteam.orchestration.state import TerminationReason, initial_graph_state
from redteam.orchestration.work.models import WorkStatus
from tests.integration.test_native_react_loop import (
    ScriptedNativeLLM,
    _engagement,
    _native_deps,
    deps_runtime,
)


class Workroom:
    """Only Docker execution is replaced; all framework boundaries are real."""

    def __init__(self):
        self.calls = []

    async def probe(self):
        return True

    async def exec_script(self, key, language, script, timeout_seconds=None, **kwargs):
        self.calls.append((key, language, script))
        return ExecResult(
            exit_code=0, stdout="pipeline-original-result", stderr="", timed_out=False,
            truncated=False, duration_ms=1, workroom_key=key, container_name="fake-docker",
        )


@pytest.mark.parametrize("requires_approval", [False, True])
async def test_shell_native_call_keeps_control_work_and_evidence(requires_approval):
    engagement = _engagement()
    engagement.approval_policy = ApprovalPolicy(
        auto_approve_up_to=None if requires_approval else RiskLevel.CRITICAL,
        require_approval_for=[SHELL_NAMESPACE] if requires_approval else [],
    )
    run_id = RunId("run_shell_pipeline")
    instance_id = AgentInstanceId("shell-a")
    key = workroom_key_for(str(run_id), str(instance_id))
    llm = ScriptedNativeLLM([NativeCompletion(tool_calls=(NativeToolCall(
        call_id="shell-original", name=exposed_tool_name(f"{SHELL_NAMESPACE}.{RUN_TOOL_NAME}"),
        arguments={"workroom_key": key, "language": "bash", "script": "printf local"},
    ),))])
    workroom = Workroom()
    adapter = BuiltinShellAdapter(
        ShellWorkroomPolicy(image="local:test", allow_unpinned_image=True), workroom=workroom,
    )
    gateway = await build_mcp_tool_gateway([], builtin_adapters=[adapter])
    role_id = AgentId("shell-test-agent")
    agents = AgentRegistry(
        specs=[AgentSpec(agent_id=role_id, name="Shell test", capabilities=[SHELL_NAMESPACE])],
        definitions=[AgentDefinition(
            agent_id=role_id, name="Shell test", role="shell-test",
            capabilities=[SHELL_NAMESPACE], provider_id="test:shell",
            context_profile=ContextProfile(profile_id="shell-test"),
        )],
    )
    deps = replace(
        _native_deps(engagement, llm), tool_catalog=gateway, tool_gateway=gateway, agents=agents,
        control_plane=DefaultControlPlane(
            {engagement.engagement_id: engagement}, outbound_descriptor_resolver=gateway.get,
        ),
    )
    await seed_from_engagement(AttackGraphService(deps.graph_store), engagement, run_id=run_id)
    runtime = deps_runtime(run_id, engagement).model_copy(update={"instance_id": instance_id})
    final = await build_graph(deps).ainvoke(initial_graph_state(runtime))
    items = await deps.work_store.list_items(run_id=run_id)
    assert len(items) == 1
    assert items[0].instance_id == instance_id
    records = await deps.evidence_store.list_for_run(run_id)
    originals = [record for record in records if record.source_type is EvidenceSourceType.TOOL]
    if requires_approval:
        assert final["runtime"].termination_reason is TerminationReason.APPROVAL_PENDING
        assert workroom.calls == [] and originals == []
    else:
        assert workroom.calls == [(key, "bash", "printf local")]
        assert items[0].status is WorkStatus.SUCCEEDED
        assert len(originals) == 1
        evidence = originals[0]
        assert evidence.metadata["work_id"] == items[0].work_id
        assert evidence.metadata["proposal"]["instance_id"] == instance_id
        assert evidence.metadata["tool_result"]["output"]["stdout"] == "pipeline-original-result"
        assert final["runtime"].actions_executed == 1
