"""Install route contract: ports, env, engine binding, strategy scope.

Owned by Capability (TASK-005). Loopback mocks only: no docker, no network
egress beyond 127.0.0.1.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import cast

import pytest

from redteam.capability.gated_network_policy import (
    GatedNetworkPolicy,
    InstallProxyConfig,
    PinnedRegistry,
)
from redteam.capability.gated_strategy import GatedExecutionPolicy
from redteam.capability.gated_window import GatedNetworkError, GatedWindowExtension
from redteam.capability.install_proxy import (
    InstallProxyRoute,
    engine_for_route,
    proxy_env,
    route_for,
)
from redteam.capability.install_proxy_engine import ProxyRefused
from redteam.core.contracts.network import (
    GrantRefused,
    NetworkBudget,
    NetworkCapacity,
    NetworkPurpose,
    WindowCaps,
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

CRLF = bytes([13, 10])


def _config() -> InstallProxyConfig:
    return InstallProxyConfig(
        endpoint="http://127.0.0.1:3128",
        pinned_hosts=("10.0.0.7",),
        mirror_upstream="https://packages.example:8443",
    )


def test_route_ports_are_separate() -> None:
    route = route_for(_config(), mirror_name="m", mirror_ips=("10.0.0.9",))
    assert route.proxy_port == 3128
    assert (route.upstream_host, route.upstream_port) == ("packages.example", 8443)
    assert route.pinned_mirror_ips == ("10.0.0.9",)


def test_route_scheme_default_upstream_port() -> None:
    config = InstallProxyConfig(
        endpoint="http://127.0.0.1:3128",
        pinned_hosts=("10.0.0.7",),
        mirror_upstream="https://packages.example",
    )
    route = route_for(config, mirror_name="m", mirror_ips=("10.0.0.9",))
    assert route.upstream_port == 443


def test_route_rejects_bad_inputs() -> None:
    config = InstallProxyConfig(
        endpoint="http://127.0.0.1:3128",
        pinned_hosts=("10.0.0.7",),
        mirror_upstream="https://packages.example",
    )
    with pytest.raises(ValueError):
        route_for(None, mirror_name="m", mirror_ips=("10.0.0.1",))
    with pytest.raises(ValueError):
        route_for(config, mirror_name=" ", mirror_ips=("10.0.0.1",))
    with pytest.raises(ValueError):
        route_for(config, mirror_name="m", mirror_ips=())
    with pytest.raises(ValueError):
        InstallProxyConfig(
            endpoint="gopher://x:70",
            pinned_hosts=("10.0.0.7",),
            mirror_upstream="https://packages.example",
        )
    with pytest.raises(ValueError):
        InstallProxyConfig(
            endpoint="http://127.0.0.1:3128",
            pinned_hosts=("10.0.0.7",),
            mirror_upstream="not a url",
        )


def test_proxy_env_is_forward_proxy_only() -> None:
    route = route_for(_config(), mirror_name="m", mirror_ips=("10.0.0.9",))
    env = proxy_env(route)
    assert env["HTTP_PROXY"] == "http://127.0.0.1:3128"
    assert env["HTTPS_PROXY"] == "http://127.0.0.1:3128"
    assert env["http_proxy"] == "http://127.0.0.1:3128"
    assert env["https_proxy"] == "http://127.0.0.1:3128"
    assert "PIP_INDEX_URL" not in env
    assert "npm_config_registry" not in env
    assert "PIP_TRUSTED_HOST" not in env


def test_engine_for_route_keying_and_refusal() -> None:
    route = route_for(_config(), mirror_name="m", mirror_ips=("10.0.0.9",))
    engine = engine_for_route(route, names=("m", "packages.example"))
    assert engine.pins["10.0.0.9"] == ("10.0.0.9",)
    assert engine.pins["m"] == ("10.0.0.9",)
    assert engine.pins["packages.example"] == ("10.0.0.9",)
    bad = InstallProxyRoute(
        endpoint="http://127.0.0.1:3128",
        proxy_port=3128,
        upstream_host="packages.example",
        upstream_port=8443,
        pinned_mirror_ips=("0:0:0:0:0:0:0:1",),
    )
    with pytest.raises(ProxyRefused):
        engine_for_route(bad)








def _install_policy():
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
        max_flow_init_deliveries=4,
        max_output_bytes=4096,
        duration_seconds=60.0,
        purpose=NetworkPurpose.INSTALL,
    )
    return GatedNetworkPolicy(
        budget=budget,
        default_target_caps=target,
        default_install_caps=install,
        registry=PinnedRegistry(targets={"t": ("10.0.0.9",)}, mirrors={"m": ("10.0.0.10",)}),
        install_proxy=InstallProxyConfig(
            endpoint="http://127.0.0.1:3128",
            pinned_hosts=("10.0.0.7",),
            mirror_upstream="https://packages.example:8443",
        ),
    )


def test_install_scope_uses_upstream_port_not_proxy_port() -> None:
    seen = []

    def validator(target, *, denials_only=False):
        seen.append((target.host, target.port, denials_only))
        return True, [], None

    class _Ext:
        def __init__(self, policy):
            self.policy = policy

    extension = cast(GatedWindowExtension, _Ext(_install_policy()))
    policy = GatedExecutionPolicy(extension, scope_validator=validator)
    context = ExecutionContext(
        engagement_id=EngagementId("eng_001"),
        run_id=RunId("run_001"),
        instance_id=AgentInstanceId("inst_001"),
        approved_target=DomainTarget(domain="m"),
    )
    invocation = ToolInvocation(
        invocation_id=InvocationId("inv_001"),
        action_id=ActionId("act_001"),
        run_id=RunId("run_001"),
        tool_id=ToolId("analysis.shell_install_run"),
        arguments={"language": "bash", "script": "true", "target_ref": "m"},
        execution_context=context,
        created_at=datetime.now(timezone.utc),
    )
    route = policy.resolve(invocation)
    assert not isinstance(route, GrantRefused)
    assert route.scope_target.port == 8443
    assert route.allowed and all(flow.port == 3128 for flow in route.allowed)
    mirror_denials = [entry for entry in seen if entry[0] == "10.0.0.10"]
    assert mirror_denials and all(entry[1] == 8443 for entry in mirror_denials)
    proxy_denials = [entry for entry in seen if entry[0] == "10.0.0.7"]
    assert proxy_denials and all(entry[1] == 3128 for entry in proxy_denials)
    install_route = policy.install_route_for("m")
    assert (install_route.upstream_host, install_route.upstream_port) == (
        "packages.example",
        8443,
    )
    env = policy.install_forward_env(install_route)
    assert env["HTTPS_PROXY"] == "http://127.0.0.1:3128"
    with pytest.raises(GatedNetworkError):
        policy.install_route_for("unknown-mirror")
