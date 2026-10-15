"""Opt-in direct Docker target-route proof on test-owned resources.

Scope of this file (not production bootstrap):
- Real shell runtime preparation for an explicit pinned local Kali image ID.
- Real direct target preparation (`prepare_target_network`) for an owned bridge.
- Real `configure_direct_target` and the real capability gateway
  (`invoke_with_admission`) with the real direct execution policy.
- The `before_start` admission hook is a pass-through owned fixture. No
  Control Plane decision is made here; scope is enforced inside the direct
  execution policy through `check_target_scope`.
- Not covered: `build_application` / `open_central_run` selection of this
  provider. That path is a separate integration.

Resources are labelled with a per-run unique tag and removed by that label.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import shutil
import uuid
from datetime import datetime, timezone

import pytest

from redteam.capability import shell_preparation
from redteam.capability.bootstrap import build_mcp_tool_gateway
from redteam.capability.direct_target import (
    SHELL_TARGET_RUN,
    DirectTargetConfig,
    configure_direct_target,
    prepare_target_network,
)
from redteam.capability.shell_manifest import SHELL_NAMESPACE
from redteam.capability.shell_runtime import prepare_shell_runtime
from redteam.capability.shell_workroom import workroom_key_for
from redteam.core.contracts.engagement import EngagementScope
from redteam.core.contracts.target import EndpointTarget
from redteam.core.contracts.tool import ExecutionContext, ToolInvocation
from redteam.core.ids import ActionId, AgentInstanceId, EngagementId, InvocationId, RunId, ToolId
from tests.integration.test_gated_window_docker import NETWORK_FIXTURE, docker

PROOF_ENV = "REDTEAM_DIRECT_TARGET_PROOF"
KALI_IMAGE = "sha256:a85c97d7b47bf7c0cc290e380c74dda9a8c5a4a6374658183adc0a7821ecb7b7"
MOCK_IMAGE = os.environ.get("SHELL_TEST_IMAGE", "python:3.11-slim")
DOCKER_CONTEXT = "desktop-linux"
TARGET_PORT = 18080
LABEL_KEY = "io.whs-redteam.direct-target-proof"
RECEIPT_DIR = "/tmp/receipt"
DENIED_NAME = "denied.owned.test"

pytestmark = [
    pytest.mark.skipif(os.environ.get(PROOF_ENV) != "1",
                       reason="explicit REDTEAM_DIRECT_TARGET_PROOF=1 required"),
    pytest.mark.skipif(shutil.which("docker") is None, reason="docker unavailable"),
]

ALLOWED_SCRIPT = """
import socket
with socket.create_connection(({host!r}, {port}), timeout=3) as connection:
    connection.sendall(b"direct-proof")
    reply = connection.recv(32)
assert reply == b"ok", reply
print("DIRECT-OK", reply.decode())
"""


async def _docker_text(*argv: str) -> tuple[int, str, str]:
    code, out, err = await docker(*argv)
    return code, out.decode(errors="replace").strip(), err.decode(errors="replace")


async def _owned_ids(kind: str, tag: str) -> list[str]:
    selector = f"label={LABEL_KEY}={tag}"
    if kind == "container":
        code, out, _ = await _docker_text("ps", "-aq", "--filter", selector)
    else:
        code, out, _ = await _docker_text("network", "ls", "-q", "--filter", selector)
    assert code == 0
    return out.split()


async def _receipt(container: str, name: str) -> str:
    code, out, _ = await _docker_text("exec", container, "cat", f"{RECEIPT_DIR}/{name}")
    return out if code == 0 else ""


async def _wait_for_mock(container: str) -> None:
    for _ in range(40):
        code, _, _ = await _docker_text("exec", container, "test", "-e",
                                        f"{RECEIPT_DIR}/mock-tcp.log")
        if code == 0:
            return
        await asyncio.sleep(0.25)
    raise AssertionError("mock receipt file never appeared")


async def _attached_networks(container: str) -> dict:
    code, out, err = await _docker_text(
        "inspect", "--format", "{{json .NetworkSettings.Networks}}", container
    )
    assert code == 0, err[:300]
    return json.loads(out)


async def test_direct_target_route_allowed_call_and_refusals(tmp_path, monkeypatch):
    if os.environ.get("DOCKER_HOST") and not os.environ.get("DOCKER_CONTEXT"):
        pytest.skip("choose a named DOCKER_CONTEXT for this proof")
    code, _, _ = await _docker_text("image", "inspect", "--format", "{{.Id}}", MOCK_IMAGE)
    if code != 0:
        pytest.skip("mock image not present locally: " + MOCK_IMAGE)
    code, image_id, _ = await _docker_text("image", "inspect", "--format", "{{.Id}}", KALI_IMAGE)
    if code != 0 or image_id != KALI_IMAGE:
        pytest.skip("pinned Kali image ID not present locally")

    # Keep preparation records out of the operator's real cache directory.
    cache = tmp_path / "prep-cache"
    monkeypatch.setattr(shell_preparation, "_cache_directory", lambda: cache)
    policy_path = tmp_path / "shell-policy.json"
    policy_path.write_text(json.dumps({"image": KALI_IMAGE, "docker_context": DOCKER_CONTEXT}))

    tag = uuid.uuid4().hex[:12]
    label = f"{LABEL_KEY}={tag}"
    network_name = "direct-proof-net-" + tag
    mock_name = "direct-proof-mock-" + tag
    run_id = RunId("direct_" + tag)
    instance_id = AgentInstanceId("worker")
    key = workroom_key_for(str(run_id), str(instance_id))
    runtime = None
    try:
        runtime = await prepare_shell_runtime(
            str(policy_path), docker_context=DOCKER_CONTEXT
        )
        assert runtime is not None and runtime.environment_fingerprint

        code, network_id, err = await _docker_text(
            "network", "create", "--driver", "bridge", "--internal", "--label", label, network_name
        )
        assert code == 0, err[:300]
        code, _, err = await _docker_text(
            "run", "-d", "--name", mock_name, "--network", network_name, "--label", label,
            "--cap-drop", "ALL", MOCK_IMAGE, "sleep", "infinity",
        )
        assert code == 0, err[:300]
        code, _, err = await _docker_text("cp", str(NETWORK_FIXTURE), mock_name + ":/tmp/mock.py")
        assert code == 0, err[:300]
        code, _, err = await _docker_text(
            "exec", "-d", mock_name, "python3", "/tmp/mock.py", "serve",
            "--bind", "0.0.0.0", "--tcp-port", str(TARGET_PORT),
            "--udp-port", "18081", "--dns-port", "18053", "--log-dir", RECEIPT_DIR,
        )
        assert code == 0, err[:300]
        await _wait_for_mock(mock_name)
        code, mock_ip, err = await _docker_text(
            "inspect", "--format", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
            mock_name,
        )
        assert code == 0 and mock_ip, err[:300]
        ipaddress.IPv4Address(mock_ip)

        # The denied name shares the owned mock IP; scope, not reachability, must refuse it.
        network = DirectTargetConfig(
            targets={mock_ip: (mock_ip,), DENIED_NAME: (mock_ip,)},
            target_ports={mock_ip: (TARGET_PORT,), DENIED_NAME: (TARGET_PORT,)},
            target_network_id=network_id,
        )
        prepared = await prepare_target_network(
            runtime.policy, network, shell_fingerprint=runtime.environment_fingerprint
        )
        scope = EngagementScope(allowed_hosts=[mock_ip + ":" + str(TARGET_PORT)])
        _extension, _executor, descriptors = configure_direct_target(
            runtime, scope, prepared.provider
        )
        assert [d.tool_id for d in descriptors] == [
            ToolId(SHELL_NAMESPACE + "." + SHELL_TARGET_RUN)
        ]
        gateway = await build_mcp_tool_gateway(
            [],
            builtin_adapters=[runtime.adapter],
            builtin_output_limits=runtime.output_limits,
            health_refresh_enabled=False,
        )

        async def admission_passthrough() -> None:
            # Owned-fixture hook: makes no Control decision; no rejection is raised.
            return None

        def invoke(suffix: str, target_ref: str, port: int, approved_host: str, script: str):
            invocation = ToolInvocation(
                invocation_id=InvocationId("inv_" + tag + "_" + suffix),
                action_id=ActionId("act_" + tag + "_" + suffix),
                run_id=run_id,
                tool_id=ToolId(SHELL_NAMESPACE + "." + SHELL_TARGET_RUN),
                binding_mode="selected_tool",
                timeout_seconds=60,
                arguments={
                    "target_ref": target_ref,
                    "port": port,
                    "language": "python3",
                    "script": script,
                },
                execution_context=ExecutionContext(
                    engagement_id=EngagementId("direct-proof"),
                    run_id=run_id,
                    instance_id=instance_id,
                    approved_target=EndpointTarget(host=approved_host, port=port),
                ),
                created_at=datetime.now(timezone.utc),
            )
            return gateway.invoke_with_admission(invocation, admission_passthrough)

        def room() -> str:
            assert key in runtime.adapter.program_source._known
            return runtime.adapter.program_source._known[key]

        receipt_before = await _receipt(mock_name, "mock-tcp.log")
        assert receipt_before.count("ACCEPT") == 0, receipt_before

        allowed_script = ALLOWED_SCRIPT.format(host=mock_ip, port=TARGET_PORT)
        allowed = await invoke("allowed", mock_ip, TARGET_PORT, mock_ip, allowed_script)
        assert allowed.success and allowed.output["exit_code"] == 0, allowed
        assert "DIRECT-OK" in allowed.output["stdout"], allowed.output
        assert allowed.metadata["direct_target_network"] == "attached_for_execution"
        assert allowed.metadata["network_enforcement"] == "none"
        room_name = room()
        receipt_after = await _receipt(mock_name, "mock-tcp.log")
        assert receipt_after.count("ACCEPT") == 1 and "DATA bytes=12" in receipt_after
        assert await _attached_networks(room_name) == {}, "workroom must detach after the call"
        # Workroom's own capability set is unchanged; the direct route adds no NET_ADMIN/NET_RAW.
        code, cap_add, err = await _docker_text(
            "inspect", "--format", "{{json .HostConfig.CapAdd}}", room_name
        )
        assert code == 0
        granted = set(json.loads(cap_add) or [])
        assert not granted & {"NET_ADMIN", "NET_RAW"}, granted

        # Unselected port: the schema or the direct policy must refuse before any attach.
        refused_port = await invoke(
            "port", mock_ip, TARGET_PORT + 1, mock_ip,
            ALLOWED_SCRIPT.format(host=mock_ip, port=TARGET_PORT + 1),
        )
        assert not refused_port.success and refused_port.output is None, refused_port
        # Out-of-scope name on the selected IP: the direct policy must refuse with the scope code.
        refused_scope = await invoke(
            "scope", DENIED_NAME, TARGET_PORT, DENIED_NAME, allowed_script
        )
        assert not refused_scope.success and refused_scope.output is None, refused_scope
        assert refused_scope.error.code == "direct_target_scope_denied", refused_scope.error

        receipt_final = await _receipt(mock_name, "mock-tcp.log")
        assert receipt_final.count("ACCEPT") == 1, "refused calls must not reach the mock"
        assert await _attached_networks(room_name) == {}
    finally:
        if runtime is not None:
            await runtime.adapter.program_source.remove_workroom(key)
        for cid in await _owned_ids("container", tag):
            await _docker_text("rm", "-f", cid)
        for nid in await _owned_ids("network", tag):
            await _docker_text("network", "rm", nid)
        assert await _owned_ids("container", tag) == []
        assert await _owned_ids("network", tag) == []
