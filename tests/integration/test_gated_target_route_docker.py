"""Opt-in real TCP/UDP proof on test-owned Docker resources; no LLM or live targets."""

import json
import os
import uuid
from datetime import datetime, timezone

import pytest

from redteam.capability.bootstrap import build_mcp_tool_gateway
from redteam.capability.gated_activation import GatedProviderConfig, configure_gated
from redteam.capability.gated_network_policy import PinnedRegistry
from redteam.capability.gated_routing import validate_target_route
from redteam.capability.gated_window import build_ruleset
from redteam.capability.shell_policy import ShellWorkroomPolicy
from redteam.capability.shell_runtime import build_shell_runtime
from redteam.capability.shell_workroom import workroom_key_for
from redteam.control.network_ledger import SQLiteNetworkGrantLedger
from redteam.core.contracts.engagement import EngagementScope
from redteam.core.contracts.target import EndpointTarget
from redteam.core.contracts.tool import ExecutionContext, ToolInvocation
from redteam.core.ids import ActionId, AgentInstanceId, EngagementId, InvocationId, RunId, ToolId
from redteam.core.scope_rules import check_target_scope
from redteam.evidence.stores.filesystem import FilesystemEvidenceStore
from tests.integration.test_gated_window_docker import (
    NETWORK_FIXTURE,
    ROOM_IMG,
    _mock,
    docker,
    image_pin,
    make_policy,
    sidecar_image,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("REDTEAM_DOCKER_ROUTE_PROOF") != "1",
    reason="explicit REDTEAM_DOCKER_ROUTE_PROOF=1 required for owned Docker packet proof",
)
_EXPLICIT_GATE_TEST_IMAGE = os.environ.get("GATE_TEST_IMAGE")


async def test_selected_bridge_reaches_mock_but_denies_other_destination_and_port(tmp_path):
    if os.environ.get("DOCKER_HOST") and not os.environ.get("DOCKER_CONTEXT"):
        pytest.skip("choose a named DOCKER_CONTEXT for this proof")
    sidecar = (
        await image_pin(_EXPLICIT_GATE_TEST_IMAGE) if _EXPLICIT_GATE_TEST_IMAGE is not None
        else await sidecar_image()
    )
    if sidecar is None:
        pytest.skip("sidecar image could not be built on this Docker endpoint")
    code, out, _ = await docker("image", "inspect", "--format", "{{.Id}}", ROOM_IMG)
    if code != 0 or not out.strip():
        pytest.skip("required already-local fixture image missing: " + ROOM_IMG)
    code, _, error = await docker(
        "run",
        "--rm",
        "-i",
        "--pull=never",
        "--network",
        "none",
        "--cap-drop",
        "ALL",
        "--cap-add",
        "NET_ADMIN",
        "--entrypoint",
        "nft",
        sidecar,
        "--check",
        "-f",
        "-",
        stdin=build_ruleset(queue_num=7, allowed=(), quota_bytes=20000).encode(),
    )
    assert code == 0, "actual nft rules unavailable on selected daemon: " + error.decode()[:300]
    code, context, _ = await docker("context", "show")
    assert code == 0
    runtime = build_shell_runtime(
        None,
        policy=ShellWorkroomPolicy(
            image=await image_pin(ROOM_IMG),
            docker_context=context.decode().strip(),
        ),
    )
    assert runtime is not None
    suffix = uuid.uuid4().hex[:16]
    network_name = "route-proof-" + suffix
    run_id = RunId("route_" + suffix)
    instance_id = AgentInstanceId("worker")
    key = workroom_key_for(str(run_id), str(instance_id))
    owned_mocks = ["t005-gated-mock-" + suffix + "a", "t005-gated-mock-" + suffix + "b"]
    assembly = None
    created = False
    try:
        code, network, err = await docker(
            "network",
            "create",
            "--internal",
            "--driver",
            "bridge",
            "--label",
            "redteam-route-proof",
            network_name,
        )
        assert code == 0, err.decode()[:300]
        created = True
        network_id = network.decode().strip()
        _, allowed_ip = await _mock(suffix + "a", network_name, NETWORK_FIXTURE)
        _, denied_ip = await _mock(suffix + "b", network_name, NETWORK_FIXTURE)
        policy = make_policy(allowed_ip, sidecar_image=sidecar).model_copy(
            update={
                "registry": PinnedRegistry(targets={allowed_ip: (allowed_ip,)}),
                "target_network_id": network_id,
                "target_ports": {allowed_ip: (18080,)},
                "install_proxy": None,
            }
        )
        ledger = SQLiteNetworkGrantLedger(
            tmp_path / "network.db", engagement_id="route-proof", budget=policy.budget
        )
        await ledger.setup()
        evidence = FilesystemEvidenceStore(tmp_path / "evidence")
        identity = await validate_target_route(policy, runtime.adapter.program_source._run_docker)
        scope = EngagementScope(allowed_hosts=[allowed_ip + ":18080"])
        assembly = configure_gated(
            runtime,
            scope,
            ledger,
            evidence,
            provider_config=GatedProviderConfig(policy, target_route_identity=identity),
        )
        runtime.adapter.set_gated_policy(assembly.executor, assembly.descriptors)
        # Require replies: a successful TCP/UDP send alone is never delivery proof.
        script = f"""
import socket
with socket.create_connection(({allowed_ip!r}, 18080), timeout=2) as connection:
    connection.sendall(b"route-proof")
    assert connection.recv(32) == b"ok"
for host, port, kind in [({allowed_ip!r}, 18081, socket.SOCK_DGRAM),
                         ({denied_ip!r}, 18080, socket.SOCK_STREAM)]:
    with socket.socket(socket.AF_INET, kind) as connection:
        connection.settimeout(0.5)
        try:
            connection.connect((host, port))
            connection.sendall(b"must-not-arrive")
            response = connection.recv(32)
        except OSError:
            continue
        raise AssertionError("unselected destination/port replied: " + repr(response))
print("ALLOWED-REPLY; UNSELECTED-DESTINATION-AND-PORT-BLOCKED")
"""
        invocation = ToolInvocation(
            invocation_id=InvocationId("inv_" + suffix),
            action_id=ActionId("act_" + suffix),
            run_id=run_id,
            tool_id=ToolId("analysis.shell_target_run"),
            binding_mode="selected_tool",
            timeout_seconds=60,
            arguments={
                "target_ref": allowed_ip,
                "port": 18080,
                "language": "python3",
                "script": script,
            },
            execution_context=ExecutionContext(
                engagement_id=EngagementId("route-proof"),
                run_id=run_id,
                instance_id=instance_id,
                approved_target=EndpointTarget(host=allowed_ip, port=18080),
            ),
            created_at=datetime.now(timezone.utc),
        )
        gateway = await build_mcp_tool_gateway(
            [],
            builtin_adapters=[runtime.adapter],
            builtin_output_limits=runtime.output_limits,
            health_refresh_enabled=False,
        )

        async def admit_owned_fixture():
            # Explicit trusted fixture admission; no model or human-approval workflow.
            assert check_target_scope(invocation.execution_context.approved_target, scope)[0]

        result = await gateway.invoke_with_admission(invocation, admit_owned_fixture)
        assert result.success and result.output["exit_code"] == 0, result
        assert "ALLOWED-REPLY" in result.output["stdout"]
        assert result.metadata["gated_settlement_state"] == "released"
        records = await evidence.list_for_action(invocation.action_id)
        assert records and records[0].metadata["target_route_identity"] == identity
        assert records[0].action_id == invocation.action_id
        code, out, _ = await docker(
            "inspect",
            "--format",
            "{{json .NetworkSettings.Networks}}",
            runtime.adapter.program_source._known[key],
        )
        assert code == 0 and json.loads(out) == {}
        code, _, _ = await docker("network", "inspect", network_id)
        assert code == 0  # Operator network survives gate teardown.
    finally:
        if assembly is not None:
            for window in tuple(assembly.extension._open.values()):
                if window.rpc_task:
                    window.rpc_task.cancel()
                await docker("rm", "-f", window.container_tag)
                await docker("network", "disconnect", network_name, window.room)
        await runtime.adapter.program_source.remove_workroom(key)
        for name in owned_mocks:
            await docker("rm", "-f", name)
        if created:
            await docker("network", "rm", network_name)
