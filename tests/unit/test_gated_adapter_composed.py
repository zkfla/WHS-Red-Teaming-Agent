"""Composed gated path: real descriptors plus real adapter plus real policy.

Owned by Capability (TASK-005). Window and supervisor are minimal fakes;
source modules are composed, not reimplemented. The end-to-end gated body
case runs all three bodies with no skip: a selector-parse regression must
FAIL here, never skip. No docker, loopback only.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from redteam.capability.gated_manifest import (
    SHELL_INSTALL_RUN,
    SHELL_TARGET_JOB_START,
    SHELL_TARGET_RUN,
    build_gated_descriptors,
    gated_body_operation,
    validate_gated_descriptors,
)
from redteam.capability.gated_network_policy import (
    GatedNetworkPolicy,
    InstallProxyConfig,
    PinnedRegistry,
)
from redteam.capability.gated_strategy import GatedExecutionPolicy, project_parsed_script
from redteam.capability.shell_adapter import BuiltinShellAdapter
from redteam.capability.shell_manifest import JOB_START_OPERATION, RUN_OPERATION
from redteam.capability.shell_policy import ShellWorkroomPolicy
from redteam.capability.shell_workroom import workroom_key_for
from redteam.core.contracts.network import (
    NetworkBudget,
    NetworkCapacity,
    NetworkGrant,
    NetworkPurpose,
    WindowCaps,
    WindowSettlement,
    WindowState,
)
from redteam.core.contracts.target import DomainTarget
from redteam.core.contracts.tool import ExecutionContext, ToolInvocation
from redteam.core.ids import (
    ActionId,
    AgentInstanceId,
    EngagementId,
    InvocationId,
    RunId,
    ToolId,
)
from redteam.orchestration.engine import validate_argument_target


def _policy_cfg():
    return ShellWorkroomPolicy(image="python:3.11-slim", allow_unpinned_image=True)


def test_bodies_resolve_from_actual_descriptors() -> None:
    descriptors = build_gated_descriptors(_policy_cfg())
    validate_gated_descriptors(descriptors, _policy_cfg())
    bodies = {d.tool_name: gated_body_operation(d) for d in descriptors}
    assert bodies == {
        SHELL_TARGET_RUN: RUN_OPERATION,
        SHELL_TARGET_JOB_START: JOB_START_OPERATION,
        SHELL_INSTALL_RUN: RUN_OPERATION,
    }


def test_native_alias_binding_discriminates_domains() -> None:
    descriptors = {d.tool_name: d for d in build_gated_descriptors(None)}
    descriptor = descriptors[SHELL_TARGET_RUN]
    arguments = {"target_ref": "alpha.example", "port": 8443}
    validate_argument_target(
        DomainTarget(domain="alpha.example"),
        arguments,
        {"alpha.example"},
        operation=SHELL_TARGET_RUN,
        asset_bindings={},
        operation_bindings=None,
        argument_bindings=dict(descriptor.argument_bindings),
        target_binding=descriptor.target_binding,
    )
    with pytest.raises(ValueError):
        validate_argument_target(
            DomainTarget(domain="beta.example"),
            arguments,
            {"beta.example"},
            operation=SHELL_TARGET_RUN,
            asset_bindings={},
            operation_bindings=None,
            argument_bindings=dict(descriptor.argument_bindings),
            target_binding=descriptor.target_binding,
        )


def test_projection_preserves_invocation() -> None:
    arguments = {
        "workroom_key": "run_001.inst_001",
        "language": "bash",
        "script": "true",
        "timeout_seconds": 5.0,
        "target_ref": "alpha.example",
        "port": 8443,
    }
    before = dict(arguments)
    projected = project_parsed_script(arguments)
    assert projected == {"language": "bash", "script": "true", "timeout_seconds": 5.0}
    assert arguments == before


class _StubResult:
    def to_output_dict(self, digest):
        return {"exit_code": 0, "stdout": "ok", "stderr": "", "script_sha256": digest}


class _StubWorkroom:
    def __init__(self):
        self.bodies = []
        self.manifests = []

    async def exec_script(self, key, language, script, timeout_seconds=None, **kwargs):
        self.bodies.append(language)
        return _StubResult()

    async def ensure_workroom(self, key):
        return None

    async def write_job_manifest(self, key, job_id, manifest):
        self.manifests.append(dict(manifest))
        return None

    async def supervisor_trace_present(self, key, supervisor_id):
        return True


class _StubWindow:
    def __init__(self, network_policy):
        self.policy = network_policy
        self.opened = 0
        self.closed = 0

    async def open_window(self, **kwargs):
        self.opened += 1
        return NetworkGrant(
            window_id="nwin_composed",
            run_id=RunId("run_001"),
            instance_id=AgentInstanceId("inst_001"),
            epoch=7,
            purpose=NetworkPurpose.TARGET,
            reserved_deliveries=4,
            reserved_bytes=4096,
            deadline_epoch_seconds=9999999999.0,
        )

    async def close_window(self, key):
        self.closed += 1
        return WindowSettlement(
            window_id="nwin_composed",
            state=WindowState.RELEASED,
            charged_deliveries=0,
            charged_bytes=0,
            released_deliveries=4,
            released_bytes=4096,
            committed_deliveries=0,
            metered_bytes=0,
            metered_packets=0,
            outcome="released",
        )

    def companion_for(self, window_id):
        return {"window_id": window_id}

    def is_transmit_open(self, key):
        return False


class _StubEvidence:
    def __init__(self):
        self.records = []

    async def append(self, record):
        self.records.append(record)


def _network_policy():
    cap = NetworkCapacity(max_flow_init_deliveries=4, max_output_bytes=4096)
    budget = NetworkBudget(
        global_capacity=cap, target_capacity=cap, install_capacity=cap
    )
    target = WindowCaps(
        max_flow_init_deliveries=4,
        max_output_bytes=4096,
        duration_seconds=60.0,
        purpose=NetworkPurpose.TARGET,
    )
    install = WindowCaps(
        max_flow_init_deliveries=1,
        max_output_bytes=1024,
        duration_seconds=60.0,
        purpose=NetworkPurpose.INSTALL,
    )
    return GatedNetworkPolicy(
        budget=budget,
        default_target_caps=target,
        default_install_caps=install,
        registry=PinnedRegistry(
            targets={"alpha.example": ("10.0.0.9",)},
            mirrors={"pkg-mirror": ("10.0.0.21",)},
        ),
        install_proxy=InstallProxyConfig(
            endpoint="http://10.0.0.31:8080",
            pinned_hosts=("10.0.0.31",),
            mirror_upstream="https://pkg-mirror:443",
        ),
    )


def _allow(target, *, denials_only=False):
    return True, [], None


def _composed_invocation(name, ref, approved, extra_args, tag):
    key = workroom_key_for("run_001", "inst_001")
    context = ExecutionContext(
        engagement_id=EngagementId("eng_001"),
        run_id=RunId("run_001"),
        instance_id=AgentInstanceId("inst_001"),
        approved_target=DomainTarget(domain=approved),
    )
    arguments = {
        "workroom_key": key,
        "language": "bash",
        "script": "true",
        "target_ref": ref,
    }
    arguments.update(extra_args)
    return ToolInvocation(
        invocation_id=InvocationId("inv-" + tag),
        action_id=ActionId("act-" + tag),
        run_id=RunId("run_001"),
        tool_id=ToolId("analysis." + name),
        arguments=arguments,
        execution_context=context,
        created_at=datetime.now(timezone.utc),
    )


def test_composed_gated_run_job_install_bodies() -> None:
    """All three gated bodies execute; no skip on selector-parse regression.

    The adapter gated selector parse has landed, so every path below must
    genuinely run: any regression fails here instead of skipping. The
    original input digest survives projection (the recorded job manifest
    carries the digest of the full unstripped invocation, selectors
    included), and the native binding gate holds end to end (approved
    targets execute; a foreign ref is refused before any window opens).
    """
    from redteam.core.convert import execution_input_digest

    async def go():
        cfg = _policy_cfg()
        workroom = _StubWorkroom()
        adapter = BuiltinShellAdapter(cfg, workroom=workroom)
        window = _StubWindow(_network_policy())
        evidence = _StubEvidence()
        executor = GatedExecutionPolicy(
            window, scope_validator=_allow, evidence_store=evidence
        )
        descriptors = build_gated_descriptors(cfg)
        adapter.set_gated_policy(executor, descriptors)
        key = workroom_key_for("run_001", "inst_001")
        expected_script_digest = BuiltinShellAdapter.script_digest("bash", "true")
        opened_before = window.opened
        run_inv = _composed_invocation(
            SHELL_TARGET_RUN, "alpha.example", "alpha.example", {"port": 8443}, "gated-run"
        )
        run_result = await adapter.invoke_contextual(run_inv)
        assert run_result.success is True
        assert run_result.output["exit_code"] == 0
        assert run_result.metadata["workroom_key"] == key
        assert run_result.metadata["script_sha256"] == expected_script_digest
        job_inv = _composed_invocation(
            SHELL_TARGET_JOB_START, "alpha.example", "alpha.example", {"port": 8443},
            "gated-job",
        )
        job_result = await adapter.invoke_contextual(job_inv)
        assert job_result.success is True
        assert job_result.output["status"] == "running"
        assert job_result.output["workroom_key"] == key
        assert job_result.output["job_id"].startswith("shjob_")
        assert job_result.metadata["supervisor_id"] == "inv-gated-job"
        assert len(workroom.manifests) == 1
        assert workroom.manifests[0]["input_digest"] == execution_input_digest(job_inv)
        altered = job_inv.model_copy(
            update={"arguments": {**job_inv.arguments, "target_ref": "other.example"}}
        )
        assert execution_input_digest(altered) != execution_input_digest(job_inv)
        install_inv = _composed_invocation(
            SHELL_INSTALL_RUN, "pkg-mirror", "pkg-mirror", {}, "gated-install"
        )
        install_result = await adapter.invoke_contextual(install_inv)
        assert install_result.success is True
        assert install_result.output["exit_code"] == 0
        assert install_result.metadata["workroom_key"] == key
        for _ in range(200):
            if len(workroom.bodies) == 3:
                break
            await asyncio.sleep(0.05)
        assert workroom.bodies == ["bash", "bash", "bash"]
        assert window.opened == opened_before + 3
        foreign_inv = _composed_invocation(
            SHELL_TARGET_RUN, "beta.example", "alpha.example", {"port": 8443},
            "gated-foreign",
        )
        foreign_result = await adapter.invoke_contextual(foreign_inv)
        assert foreign_result.success is False
        assert foreign_result.error is not None
        assert foreign_result.error.code == "gated_approved_target_mismatch"
        assert window.opened == opened_before + 3
        assert len(evidence.records) >= 2

    asyncio.run(go())
