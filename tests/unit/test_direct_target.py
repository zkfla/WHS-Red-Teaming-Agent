"""Direct Kali target routing has no gate daemon or packet-accounting path."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from redteam.capability.adapters.mcp.base import MCPCallResult
from redteam.capability.direct_target import (
    DirectTargetConfig,
    DirectTargetExecutionPolicy,
    DirectTargetNetworkExtension,
    DirectTargetProviderConfig,
)
from redteam.capability.shell_policy import ShellWorkroomPolicy
from redteam.capability.shell_preparation import DockerEndpoint
from redteam.capability.shell_workroom import workroom_key_for
from redteam.core.contracts.target import EndpointTarget
from redteam.core.contracts.tool import (
    EXECUTION_UNCERTAIN_METADATA_KEY,
    ExecutionContext,
    ToolInvocation,
)
from redteam.core.ids import (
    ActionId,
    AgentInstanceId,
    EngagementId,
    InvocationId,
    RunId,
    ToolId,
)

NET = "a" * 64
IP = "10.0.0.9"
ROUTE = {
    "connection": "operator_bridge",
    "target_ips": [IP],
    "network_id": NET,
    "network_name": "vuln-bank",
    "containers": {IP: {"container_id": "b" * 64, "image_id": "sha256:" + "c" * 64}},
}
ENDPOINT = {"binary": "/usr/bin/docker", "context": "desktop-linux", "host": "unix:///docker"}


class FakeManager:
    def __init__(self):
        self.policy = ShellWorkroomPolicy(image="test", allow_unpinned_image=True)
        self._known = {}
        self.calls = []

    async def ensure_workroom(self, key):
        self._known[key] = "kali-room"
        return "kali-room"

    async def _run_docker(self, argv, stdin, timeout, limit=None):
        self.calls.append(argv)
        return 0, b"", b"", False, False


def provider():
    network = DirectTargetConfig(
        targets={"vuln-bank": (IP,)},
        target_ports={"vuln-bank": (5000,)},
        target_network_id=NET,
    )
    return DirectTargetProviderConfig(network, ROUTE, ENDPOINT)


def invocation(*, port=5000, approved_port=5000):
    return ToolInvocation(
        invocation_id=InvocationId("inv_001"),
        action_id=ActionId("act_001"),
        run_id=RunId("run_001"),
        tool_id=ToolId("analysis.shell_target_run"),
        arguments={
            "language": "bash", "script": "true",
            "target_ref": "vuln-bank", "port": port,
        },
        execution_context=ExecutionContext(
            engagement_id=EngagementId("eng_001"),
            run_id=RunId("run_001"),
            instance_id=AgentInstanceId("inst_001"),
            approved_target=EndpointTarget(host="vuln-bank", port=approved_port),
        ),
        created_at=datetime.now(timezone.utc),
    )


def allow(_target, *, denials_only=False):
    return True, [], None


def ok_result():
    now = datetime.now(timezone.utc)
    return MCPCallResult(
        success=True, started_at=now, finished_at=now,
        output={"exit_code": 0}, error=None, metadata={},
    )


async def test_operator_bridge_is_attached_for_run_and_detached_after(monkeypatch):
    manager = FakeManager()
    extension = DirectTargetNetworkExtension(manager, provider())
    monkeypatch.setattr(
        "redteam.capability.direct_target._route_identity",
        AsyncMock(return_value=ROUTE),
    )
    monkeypatch.setattr(
        "redteam.capability.direct_target.resolve_docker_endpoint",
        AsyncMock(return_value=DockerEndpoint(**ENDPOINT)),
    )
    policy = DirectTargetExecutionPolicy(extension, allow)

    result = await policy.execute(
        invocation(), {"language": "bash", "script": "true"}, AsyncMock(return_value=ok_result())
    )

    key = workroom_key_for("run_001", "inst_001")
    assert result.success
    assert result.metadata["network_enforcement"] == "none"
    assert extension.expected_networks(key) == frozenset({"none"})
    assert manager.calls == [
        ("network", "disconnect", "none", "kali-room"),
        ("network", "connect", "vuln-bank", "kali-room"),
        ("network", "disconnect", "-f", "vuln-bank", "kali-room"),
    ]


@pytest.mark.parametrize(
    "call,code",
    [
        (invocation(port=5001, approved_port=5001), "direct_target_port_unselected"),
        (invocation(port=5000, approved_port=5001), "direct_target_approved_target_mismatch"),
    ],
)
async def test_selector_mismatch_is_refused_before_network_change(call, code):
    manager = FakeManager()
    extension = DirectTargetNetworkExtension(manager, provider())
    result = await DirectTargetExecutionPolicy(extension, allow).execute(
        call, {"language": "bash", "script": "true"},
        AsyncMock(side_effect=AssertionError("must not execute")),
    )
    assert not result.success and result.error.code == code
    assert manager.calls == []


async def test_disconnect_failure_is_visible_and_route_remains_tracked(monkeypatch):
    manager = FakeManager()

    async def docker(argv, stdin, timeout, limit=None):
        manager.calls.append(argv)
        if argv[:3] == ("network", "disconnect", "-f"):
            return 1, b"", b"failed", False, False
        return 0, b"", b"", False, False

    manager._run_docker = docker
    extension = DirectTargetNetworkExtension(manager, provider())
    monkeypatch.setattr(
        "redteam.capability.direct_target._route_identity",
        AsyncMock(return_value=ROUTE),
    )
    monkeypatch.setattr(
        "redteam.capability.direct_target.resolve_docker_endpoint",
        AsyncMock(return_value=DockerEndpoint(**ENDPOINT)),
    )
    result = await DirectTargetExecutionPolicy(extension, allow).execute(
        invocation(), {"language": "bash", "script": "true"}, AsyncMock(return_value=ok_result())
    )
    key = workroom_key_for("run_001", "inst_001")
    assert not result.success and result.error.code == "direct_target_disconnect_failed"
    assert extension.expected_networks(key) == frozenset({"vuln-bank"})


async def test_failed_detach_keeps_output_marked_uncertain_and_busy(monkeypatch):
    manager = FakeManager()

    async def docker(argv, stdin, timeout, limit=None):
        manager.calls.append(argv)
        if argv[:3] == ("network", "disconnect", "-f"):
            return 1, b"", b"failed", False, False
        return 0, b"", b"", False, False

    manager._run_docker = docker
    extension = DirectTargetNetworkExtension(manager, provider())
    monkeypatch.setattr(
        "redteam.capability.direct_target._route_identity",
        AsyncMock(return_value=ROUTE),
    )
    monkeypatch.setattr(
        "redteam.capability.direct_target.resolve_docker_endpoint",
        AsyncMock(return_value=DockerEndpoint(**ENDPOINT)),
    )
    policy = DirectTargetExecutionPolicy(extension, allow)
    output = {"exit_code": 0, "stdout": "port 5000 open"}
    now = datetime.now(timezone.utc)
    delegate = AsyncMock(return_value=MCPCallResult(
        success=True, started_at=now, finished_at=now,
        output=output, error=None, metadata={},
    ))

    result = await policy.execute(
        invocation(), {"language": "bash", "script": "true"}, delegate
    )

    key = workroom_key_for("run_001", "inst_001")
    assert not result.success
    assert result.error.code == "direct_target_disconnect_failed"
    assert result.output == output
    assert result.metadata[EXECUTION_UNCERTAIN_METADATA_KEY] is True
    assert result.metadata["direct_target_network"] == "detach_unverified"
    assert extension.expected_networks(key) == frozenset({"vuln-bank"})

    second = await policy.execute(
        invocation(), {"language": "bash", "script": "true"}, delegate
    )

    assert not second.success and second.error.code == "direct_target_busy"
    assert delegate.await_count == 1
    detach_attempts = [
        call for call in manager.calls if call[:3] == ("network", "disconnect", "-f")
    ]
    assert len(detach_attempts) == 1
    assert ("network", "connect", "vuln-bank", "kali-room") not in manager.calls[2:]


async def test_changed_docker_endpoint_is_refused_before_workroom_creation(monkeypatch):
    manager = FakeManager()
    extension = DirectTargetNetworkExtension(manager, provider())
    monkeypatch.setattr(
        "redteam.capability.direct_target.resolve_docker_endpoint",
        AsyncMock(return_value=DockerEndpoint(
            binary="/usr/bin/docker", context="desktop-linux", host="unix:///changed"
        )),
    )
    result = await DirectTargetExecutionPolicy(extension, allow).execute(
        invocation(), {"language": "bash", "script": "true"},
        AsyncMock(side_effect=AssertionError("must not execute")),
    )
    assert not result.success and result.error.code == "direct_target_docker_endpoint_changed"
    assert manager._known == {} and manager.calls == []
