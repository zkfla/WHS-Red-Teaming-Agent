"""Direct Kali target routing has no gate daemon or packet-accounting path."""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from redteam.capability.adapters.mcp.base import MCPCallResult
from redteam.capability.direct_target import (
    DirectTargetConfig,
    DirectTargetExecutionPolicy,
    DirectTargetNetworkExtension,
    DirectTargetProviderConfig,
    _descriptor,
)
from redteam.capability.shell_policy import ShellWorkroomPolicy
from redteam.capability.shell_preparation import DockerEndpoint
from redteam.capability.shell_workroom import (
    ShellWorkroomManager,
    container_name_for,
    workroom_key_for,
)
from redteam.core.contracts.target import EndpointTarget
from redteam.core.contracts.tool import (
    EXECUTION_UNCERTAIN_METADATA_KEY,
    ExecutionContext,
    ToolInvocation,
    model_input_schema,
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


class _NoLock:
    def release(self) -> None:
        return None


class DockerBoundary:
    """Fake Docker CLI boundary. The real ShellWorkroomManager runs on top of it."""

    def __init__(self, *, rm_fails: bool = False, detach_fails: bool = False) -> None:
        self.live: set[str] = set()
        self.calls: list[tuple[str, ...]] = []
        self.rm_fails = rm_fails
        self.detach_fails = detach_fails

    async def run(self, argv, stdin, timeout, limit=None):
        argv = tuple(argv)
        self.calls.append(argv)
        if argv[0] == "start":
            self.live.add(argv[1])
        if argv[0] == "inspect":
            if argv[-1] in self.live:
                return 0, b"true", b"", False, False
            return 1, b"", b"No such object", False, False
        if argv[0] == "rm":
            if self.rm_fails:
                return 1, b"", b"cannot remove a running container", False, False
            self.live.discard(argv[-1])
        if argv[:3] == ("network", "disconnect", "-f") and self.detach_fails:
            return 1, b"", b"detach failed", False, False
        return 0, b"", b"", False, False


def real_manager(docker: DockerBoundary, monkeypatch) -> ShellWorkroomManager:
    manager = ShellWorkroomManager(ShellWorkroomPolicy(image="test", allow_unpinned_image=True))
    monkeypatch.setattr(manager, "_run_docker", docker.run)
    monkeypatch.setattr(manager, "_enforce_workroom_cap", AsyncMock(return_value=None))
    monkeypatch.setattr(manager, "_setup_container", AsyncMock(return_value=None))
    monkeypatch.setattr(
        "redteam.capability.shell_workroom._create_lock_path", lambda: "unused"
    )
    monkeypatch.setattr(
        "redteam.capability.shell_workroom._acquire_create_lock", lambda *_args: _NoLock()
    )
    return manager


def patch_route_and_endpoint(monkeypatch) -> None:
    monkeypatch.setattr(
        "redteam.capability.direct_target._route_identity",
        AsyncMock(return_value=ROUTE),
    )
    monkeypatch.setattr(
        "redteam.capability.direct_target.resolve_docker_endpoint",
        AsyncMock(return_value=DockerEndpoint(**ENDPOINT)),
    )


async def test_cache_lost_while_attached_still_detaches_the_attached_room(monkeypatch):
    key = workroom_key_for("run_001", "inst_001")
    docker = DockerBoundary(rm_fails=True)
    manager = real_manager(docker, monkeypatch)
    patch_route_and_endpoint(monkeypatch)
    extension = DirectTargetNetworkExtension(manager, provider())

    async def script_with_teardown():
        # Run teardown while the script is attached. The real remove_workroom pops
        # the cache entry before its rm -f, and the rm fails, so the container lives.
        assert await manager.remove_workroom(key) is False
        assert key not in manager._known
        return ok_result()

    result = await DirectTargetExecutionPolicy(extension, allow).execute(
        invocation(), {"language": "bash", "script": "true"}, script_with_teardown
    )

    assert result.success
    assert result.metadata["direct_target_network"] == "attached_for_execution"
    assert ("network", "disconnect", "-f", "vuln-bank", container_name_for(key)) in docker.calls
    assert extension.expected_networks(key) == frozenset({"none"})


async def test_cache_lost_and_detach_failure_refuses_closure(monkeypatch):
    key = workroom_key_for("run_001", "inst_001")
    docker = DockerBoundary(rm_fails=True, detach_fails=True)
    manager = real_manager(docker, monkeypatch)
    patch_route_and_endpoint(monkeypatch)
    extension = DirectTargetNetworkExtension(manager, provider())
    policy = DirectTargetExecutionPolicy(extension, allow)

    async def script_with_teardown():
        assert await manager.remove_workroom(key) is False
        return ok_result()

    result = await policy.execute(
        invocation(), {"language": "bash", "script": "true"}, script_with_teardown
    )

    assert not result.success
    assert result.error.code == "direct_target_disconnect_failed"
    assert result.metadata[EXECUTION_UNCERTAIN_METADATA_KEY] is True
    assert extension.expected_networks(key) == frozenset({"vuln-bank"})

    second = await policy.execute(
        invocation(), {"language": "bash", "script": "true"},
        AsyncMock(side_effect=AssertionError("must not execute")),
    )
    assert not second.success and second.error.code == "direct_target_busy"


async def test_native_selector_uses_declared_host_binding_despite_mcp_alias():
    from redteam.core.contracts.target import HostTarget
    from redteam.orchestration.engine import DecisionRuleError, validate_argument_target
    from redteam.orchestration.knowledge_validation import InternalAssessment, InternalTarget
    from redteam.orchestration.native_binding import resolve_native_target

    descriptor = _descriptor(FakeManager().policy, DirectTargetConfig(
        targets={IP: (IP,)}, target_ports={IP: (5000,)},
    ))
    target = HostTarget(host=IP + ":5000")
    deps = SimpleNamespace(
        graph_store=object(),
        engagement=SimpleNamespace(metadata={"target_refs": {IP + ":5000": "opaque-mcp-id"}}),
        operation_bindings=None,
    )
    material = InternalAssessment(targets={
        "host:registered": InternalTarget(node_id="host:registered", execution_target=target),
    })
    arguments = {"target_ref": IP, "port": 5000, "language": "bash", "script": "true"}
    assert await resolve_native_target(deps, descriptor, arguments, material) == target
    assert "workroom_key" not in model_input_schema(descriptor.input_schema)["properties"]
    for invalid in [{**arguments, "target_ref": "10.0.0.10"}, {**arguments, "port": 5001}]:
        with pytest.raises(DecisionRuleError, match="exactly one"):
            await resolve_native_target(deps, descriptor, invalid, material)
    # An opaque MCP selector retains its original exact reference check.
    with pytest.raises(DecisionRuleError):
        validate_argument_target(target, arguments, {"opaque-mcp-id"})
