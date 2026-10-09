"""Capability-side gated-window unit tests. No docker, no network."""

from __future__ import annotations

import io
from typing import cast

import pytest

from redteam.capability.gate_daemon import GateDaemon
from redteam.capability.gate_observer import (
    RunDocker,
    build_meter,
    check_barrier_closed,
    check_default_drop,
    check_interfaces_closed,
    check_open_posture,
    parse_named_counters,
)
from redteam.capability.gate_wire import (
    AdmissionReply,
    AdmissionRequest,
    WireError,
    decode_reply,
    decode_request,
    encode_reply,
    encode_request,
)
from redteam.capability.gated_network_policy import (
    EXTERNAL_IPV6,
    GatedNetworkPolicy,
    InstallProxyConfig,
    PinnedRegistry,
)
from redteam.capability.gated_window import (
    IPV6_EXTERNAL_UNSUPPORTED,
    AllowedFlow,
    GatedNetworkError,
    GatedWindowExtension,
    OpenWindow,
    build_closed_ruleset,
    build_ruleset,
    opaque_window_tag,
)
from redteam.capability.install_proxy import proxy_env, route_for
from redteam.core.contracts.network import (
    NetworkBudget,
    NetworkCapacity,
    NetworkGrant,
    NetworkPurpose,
    WindowCaps,
)
from redteam.core.ports.network_grants import NetworkGrantLedger


def _budget() -> NetworkBudget:
    cap = NetworkCapacity(max_flow_init_deliveries=10, max_output_bytes=100000)
    zero = NetworkCapacity(max_flow_init_deliveries=0, max_output_bytes=0)
    return NetworkBudget(global_capacity=cap, target_capacity=cap, install_capacity=zero)


def _policy() -> GatedNetworkPolicy:
    target = WindowCaps(
        max_flow_init_deliveries=4, max_output_bytes=4096,
        delivery_rate_per_second=None, duration_seconds=60.0,
        purpose=NetworkPurpose.TARGET,
    )
    install = WindowCaps(
        max_flow_init_deliveries=2, max_output_bytes=2048,
        delivery_rate_per_second=None, duration_seconds=60.0,
        purpose=NetworkPurpose.INSTALL,
    )
    return GatedNetworkPolicy(
        budget=_budget(), default_target_caps=target, default_install_caps=install,
        registry=PinnedRegistry(targets={"t": ("10.0.0.9",)}, mirrors={}),
        install_proxy=InstallProxyConfig(
            endpoint="http://proxy:8080",
            pinned_hosts=("m",),
            mirror_upstream="https://m:443",
        ),
    )


def test_wire_roundtrip() -> None:
    req = AdmissionRequest(
        window_id="w", daemon_session_id="s", seq=3, epoch=1,
        flow_key="tcp:4:1.2.3.4:5>6.7.8.9:80", ip_version=4, proto="tcp",
        dst_ip="6.7.8.9", dst_port=80,
    )
    assert decode_request(encode_request(req)) == req
    rep = AdmissionReply(seq=3, accepted=True, reason="committed")
    assert decode_reply(encode_reply(rep)) == rep


def test_wire_bounds_reject() -> None:
    with pytest.raises(WireError):
        decode_request(b"not json\n")
    with pytest.raises(WireError):
        decode_request(b"x" * 5000)
    bad = AdmissionRequest(
        window_id="w", daemon_session_id="s", seq=-1, epoch=1,
        flow_key="k", ip_version=4, proto="tcp", dst_ip="d", dst_port=80,
    )
    with pytest.raises(WireError):
        encode_request(bad)
    with pytest.raises(WireError):
        decode_reply(b"" + b"{\"v\":99,\"seq\":0,\"accepted\":false,\"reason\":\"x\"}\n")


def test_policy_disjoint_registry() -> None:
    with pytest.raises(ValueError):
        PinnedRegistry(targets={"t": ("10.0.0.9",)}, mirrors={"m": ("10.0.0.9",)})
    assert EXTERNAL_IPV6.startswith("unsupported")


def test_policy_caps_purpose_match() -> None:
    caps = WindowCaps(
        max_flow_init_deliveries=1, max_output_bytes=1,
        delivery_rate_per_second=None, duration_seconds=1.0,
        purpose=NetworkPurpose.INSTALL,
    )
    with pytest.raises(ValueError):
        GatedNetworkPolicy(
            budget=_budget(), default_target_caps=caps,
            default_install_caps=caps,
        )


def test_ruleset_drop_and_explicit_denies() -> None:
    text = build_ruleset(
        queue_num=7, quota_bytes=4096,
        allowed=(AllowedFlow(ip="10.0.0.9", port=443, proto="tcp"),),
    )
    assert "policy drop" in text
    assert "queue num 7" in text
    assert "ip daddr 10.0.0.9 tcp dport 443 jump meter_accept" in text
    assert "quota name qb drop" in text
    assert "counter name met_out accept" in text
    assert "denied_dns_local" in text and "denied_dns_ext" in text
    assert "denied_v6" in text
    assert "established,related accept" in text
    assert "ip6 daddr" in text
    assert "ip6 daddr ::/0 counter name denied_v6 drop" in text
    assert "table inet gated" in text
    open_report = check_default_drop(text, table="gated")
    assert not open_report.closed
    closed_text = build_closed_ruleset()
    assert check_default_drop(closed_text, table="gated").closed


def test_ruleset_rejects_ipv6_allow() -> None:
    with pytest.raises(GatedNetworkError) as exc:
        build_ruleset(
            queue_num=7, quota_bytes=10,
            allowed=(AllowedFlow(ip="::1", port=80, proto="tcp"),),
        )
    assert IPV6_EXTERNAL_UNSUPPORTED in str(exc.value)
    closed = build_closed_ruleset()
    assert check_default_drop(closed, table="gated").closed


def test_interfaces_closed() -> None:
    addr = "1: lo    inet 127.0.0.1/8 scope host valid_lft forever\n"
    assert check_interfaces_closed(addr, "").closed
    addr2 = addr + "2: eth0    inet 172.22.0.3/16 scope global eth0 valid_lft forever\n"
    route = "default via 172.22.0.1 dev eth0 \n172.22.0.0/16 dev eth0 scope link \n"
    assert check_interfaces_closed(addr2, route).closed
    bad_route = "default via 9.9.9.9 dev eth1 \n"
    assert not check_interfaces_closed(addr2, bad_route).closed
    addr3 = addr2 + "3: eth1    inet 172.22.0.4/16 scope global eth1 valid_lft forever\n"
    assert not check_interfaces_closed(addr3, route).closed


def test_counters_meter_outbound_only() -> None:
    ruleset = "table inet x {\n counter met_out {\n packets 7 bytes 512 \n }\n}\n"
    counters = parse_named_counters(ruleset)
    assert counters["met_out"].packets == 7
    meter = build_meter(ruleset, byte_counter="met_out", packet_counter="met_out")
    assert meter.metered_bytes == 512 and meter.metered_packets == 7
    with pytest.raises(ValueError):
        build_meter("table inet x {}", byte_counter="met_out", packet_counter="met_out")


def test_opaque_tag_stable() -> None:
    assert opaque_window_tag("r", "i", 1) == opaque_window_tag("r", "i", 1)
    assert opaque_window_tag("r", "i", 1) != opaque_window_tag("r", "i", 2)
    assert opaque_window_tag("r", "i", 1).startswith("gwin-")


class _FakeLedger:
    def __init__(self) -> None:
        self.settles: list[tuple] = []


class _StubManager:
    """Structural stand-in: construction only, never executes."""

    async def ensure_workroom(self, key: str) -> str:
        raise AssertionError("no workrooms here")


async def _never_docker(
    argv: tuple[str, ...],
    stdin_bytes: bytes | None,
    timeout_seconds: float,
    output_limit: int | None = None,
) -> tuple[int | None, bytes, bytes, bool, bool]:
    raise AssertionError("no docker here")


def _stub_manager():
    from redteam.capability.shell_workroom import ShellWorkroomManager

    return cast(ShellWorkroomManager, _StubManager())


def _stub_runner() -> RunDocker:
    return cast(RunDocker, _never_docker)

    async def request_window(self, **kwargs):
        raise AssertionError("not used here")

    async def admit_delivery(self, **kwargs):
        raise AssertionError("not used here")

    async def settle_window(self, *, window_id, meter, verified_closed):
        from redteam.core.contracts.network import WindowSettlement, WindowState
        self.settles.append((window_id, meter, verified_closed))
        return WindowSettlement(
            window_id=window_id, state=WindowState.QUARANTINED,
            charged_deliveries=4, charged_bytes=4096,
            released_deliveries=0, released_bytes=0,
            committed_deliveries=1, metered_bytes=None, metered_packets=None,
            outcome="unknown_fully_charged",
        )

    async def release_quarantine(self, *, window_id, verified_closed):
        return False


def test_open_refused_without_policy() -> None:
    import asyncio

    async def go() -> None:
        ext = GatedWindowExtension(
            manager=_stub_manager(),
            ledger=cast(NetworkGrantLedger, _FakeLedger()),
            run_docker=_stub_runner(),
            policy=None,
        )
        with pytest.raises(GatedNetworkError) as exc:
            await ext.open_window(
                run_id="r",
                instance_id="i",
                epoch=1,
                holder_action_id="act_001",
                holder_invocation_id="inv_001",
            )
        assert "gated_network_unconfigured" in str(exc.value)

    asyncio.run(go())


def test_busy_and_inventory_modes() -> None:
    ext = GatedWindowExtension(
        manager=_stub_manager(),
        ledger=cast(NetworkGrantLedger, _FakeLedger()),
        run_docker=_stub_runner(),
        policy=_policy(),
    )
    assert not ext.is_transmit_open("k")
    assert ext.inventory_mode("k") == "full"
    ext._opening.add("k")
    assert ext.is_transmit_open("k")
    assert ext.inventory_mode("k") == "static"
    try:
        ext.check_exec_allowed("k")
        raise AssertionError("must refuse")
    except GatedNetworkError as exc:
        assert "window_busy" in str(exc)
    ext.check_exec_allowed("other")


def test_active_window_admits_only_holder_and_exact_owned_network() -> None:
    ext = GatedWindowExtension(manager=_stub_manager(),
        ledger=cast(NetworkGrantLedger, _FakeLedger()), run_docker=_stub_runner(),
        policy=_policy())
    grant = NetworkGrant(window_id="w", run_id="r", instance_id="i", epoch=1,
        purpose=NetworkPurpose.TARGET, reserved_deliveries=4, reserved_bytes=4096,
        deadline_epoch_seconds=1000)
    window = OpenWindow(key="k", room="room", grant=grant, daemon_session_id="s",
        container_tag="tag", network_name="owned", holder_action_id="action",
        holder_invocation_id="holder")
    ext._open["k"] = window
    assert ext.expected_networks("k") == frozenset({"owned"})
    assert ext.expected_networks("other") == frozenset({"none"})
    ext.check_exec_allowed("k", invocation_id="holder")
    for caller in (None, "foreign"):
        with pytest.raises(GatedNetworkError, match="window_busy"):
            ext.check_exec_allowed("k", invocation_id=caller)
    window.settled = True
    assert ext.expected_networks("k") == frozenset({"none"})
    with pytest.raises(GatedNetworkError, match="window_busy"):
        ext.check_exec_allowed("k", invocation_id="holder")


def test_install_route_needs_config() -> None:
    with pytest.raises(ValueError):
        route_for(None, mirror_name="m", mirror_ips=("10.0.0.1",))
    route = route_for(
        _policy().install_proxy,
        mirror_name="m",
        mirror_ips=_policy().registry.mirror_ips() or ("10.0.0.5",),
    )
    env = proxy_env(route)
    assert env["HTTP_PROXY"] == "http://proxy:8080"
    assert env["HTTPS_PROXY"] == "http://proxy:8080"
    assert "PIP_INDEX_URL" not in env and "npm_config_registry" not in env
    assert route.proxy_port == 8080
    assert (route.upstream_host, route.upstream_port) == ("m", 443)


class _FakePacket:
    packet_id = 1
    ip_version = 4
    proto = "tcp"
    src_ip = "172.22.0.3"
    src_port = 40000
    dst_ip = "10.0.0.9"
    dst_port = 443

    def __init__(self) -> None:
        self.accepted = False
        self.dropped = False

    def apply_accept(self) -> None:
        self.accepted = True

    def apply_drop(self) -> None:
        self.dropped = True


class _FakeBackend:
    def bind(self, queue_num, callback) -> None:
        self.callback = callback

    def run(self) -> None:
        pass

    def unbind(self) -> None:
        pass


def test_daemon_commit_mark_accept_order() -> None:
    replies = [encode_reply(AdmissionReply(seq=1, accepted=True, reason="committed"))]
    daemon = GateDaemon(
        window_id="w", epoch=1, daemon_session_id="s", backend=_FakeBackend(),
        stdin=io.BytesIO(b"".join(replies)), stdout=io.BytesIO(),
    )
    packet = _FakePacket()
    accepted, reason = daemon.on_packet(packet)
    assert accepted and reason == "committed"
    assert packet.accepted and not packet.dropped


def test_daemon_drop_on_rpc_failure() -> None:
    daemon = GateDaemon(
        window_id="w", epoch=1, daemon_session_id="s", backend=_FakeBackend(),
        stdin=io.BytesIO(b""), stdout=io.BytesIO(),
    )
    packet = _FakePacket()
    accepted, _reason = daemon.on_packet(packet)
    assert not accepted and packet.dropped


def test_daemon_drop_on_refusal() -> None:
    replies = [encode_reply(AdmissionReply(seq=1, accepted=False, reason="denied"))]
    daemon = GateDaemon(
        window_id="w", epoch=1, daemon_session_id="s", backend=_FakeBackend(),
        stdin=io.BytesIO(b"".join(replies)), stdout=io.BytesIO(),
    )
    packet = _FakePacket()
    accepted, reason = daemon.on_packet(packet)
    assert not accepted and reason == "denied" and packet.dropped


def test_manager_busy_deny_hook() -> None:
    import asyncio

    from redteam.capability.shell_policy import ShellWorkroomPolicy
    from redteam.capability.shell_workroom import (
        ExecutionAdmission,
        ShellWorkroomError,
        ShellWorkroomManager,
    )
    class Busy(ExecutionAdmission):
        def check_exec_allowed(self, key: str, *, invocation_id: str | None = None) -> None:
            raise ShellWorkroomError("window_busy")
    policy = ShellWorkroomPolicy(image="python:3.11-slim", allow_unpinned_image=True)
    manager = ShellWorkroomManager(policy)
    manager.set_execution_admission(Busy())
    with pytest.raises(ShellWorkroomError) as exc:
        asyncio.run(manager.exec_script("run1.inst1", "bash", "echo hi"))
    assert "window_busy" in str(exc.value)


def test_open_posture_matches_authorized_window() -> None:
    text = build_ruleset(
        queue_num=7, quota_bytes=4096,
        allowed=(AllowedFlow(ip="10.0.0.9", port=443, proto="tcp"),),
    )
    report = check_open_posture(
        text, table="gated", allowed=(("10.0.0.9", 443, "tcp"),),
        queue_num=7, meter_name="met_out",
    )
    assert report.closed is True
    tampered = text.replace("10.0.0.9", "10.0.0.99")
    denied = check_open_posture(
        tampered, table="gated", allowed=(("10.0.0.9", 443, "tcp"),),
        queue_num=7, meter_name="met_out",
    )
    assert denied.closed is False
    assert check_default_drop(text, table="gated").closed is False


def test_barrier_freezes_output() -> None:
    closed_text = build_closed_ruleset()
    assert check_default_drop(closed_text, table="gated").closed is True
    assert check_barrier_closed(closed_text, table="gated").closed is False
    barrier_text = closed_text.replace(
        "table inet gated {",
        "table inet gated {\n  chain barrier "
        + "{ type filter hook output priority -10; policy drop; }",
    )
    gated = check_barrier_closed(barrier_text, table="gated")
    assert gated.closed is True
    kernel_rendered = closed_text.replace(
        "table inet gated {",
        "table inet gated {\n  chain barrier "
        + "{ type filter hook output priority filter - 10; policy drop; }",
    )
    assert check_barrier_closed(kernel_rendered, table="gated").closed is True
