"""Gated-window docker proof (own t005-gated-* resources, mocks only)."""

from __future__ import annotations

import asyncio
import os
import shutil
import uuid

import pytest

from redteam.capability.gate_observer import parse_named_counters
from redteam.capability.gated_network_policy import (
    GatedNetworkPolicy,
    InstallProxyConfig,
    PinnedRegistry,
)
from redteam.capability.gated_window import (
    AllowedFlow,
    GatedWindowExtension,
)
from redteam.capability.shell_policy import ShellWorkroomPolicy
from redteam.capability.shell_workroom import (
    ShellWorkroomManager,
    container_name_for,
    workroom_key_for,
)
from redteam.control.network_ledger import SQLiteNetworkGrantLedger
from redteam.core.contracts.network import (
    NetworkBudget,
    NetworkCapacity,
    NetworkPurpose,
    WindowCaps,
    WindowState,
)

ROOM_IMG = os.environ.get("SHELL_TEST_IMAGE", "python:3.11-slim")
SIDECAR_IMG = "t005-gate-sidecar"
pytestmark = pytest.mark.skipif(shutil.which("docker") is None, reason="docker unavailable")


async def docker(*argv, stdin=None, timeout=60.0):
    proc = await asyncio.create_subprocess_exec(
        "docker",
        *argv,
        stdin=asyncio.subprocess.PIPE if stdin else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(stdin), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise
    return proc.returncode, out, err


async def image_available(name):
    code, out, _err = await docker("images", "-q", name)
    return code == 0 and bool(out.strip())


def make_policy(mock_ip):
    cap = NetworkCapacity(max_flow_init_deliveries=100, max_output_bytes=1000000)
    zero = NetworkCapacity(max_flow_init_deliveries=0, max_output_bytes=0)
    budget = NetworkBudget(global_capacity=cap, target_capacity=cap, install_capacity=zero)
    target_caps = WindowCaps(
        max_flow_init_deliveries=30,
        max_output_bytes=20000,
        delivery_rate_per_second=None,
        duration_seconds=300.0,
        purpose=NetworkPurpose.TARGET,
    )
    install_caps = WindowCaps(
        max_flow_init_deliveries=1,
        max_output_bytes=1024,
        delivery_rate_per_second=None,
        duration_seconds=60.0,
        purpose=NetworkPurpose.INSTALL,
    )
    registry = PinnedRegistry(targets={"mock-target": (mock_ip,)}, mirrors={})
    proxy = InstallProxyConfig(
        endpoint="http://mock-proxy:8080",
        pinned_hosts=("mock-proxy",),
        mirror_upstream="https://mock-mirror:443",
    )
    return GatedNetworkPolicy(
        budget=budget,
        default_target_caps=target_caps,
        default_install_caps=install_caps,
        registry=registry,
        install_proxy=proxy,
    )


async def test_install_zero_capacity_refused(tmp_path):
    policy = make_policy("10.0.0.9")
    ledger = SQLiteNetworkGrantLedger(
        str(tmp_path / "net.db"), engagement_id="e1", budget=policy.budget
    )
    await ledger.setup()
    refused = await ledger.request_window(
        run_id="r", instance_id="i", epoch=1, caps=policy.default_install_caps
    )
    assert hasattr(refused, "code")
    granted = await ledger.request_window(
        run_id="r", instance_id="i", epoch=1, caps=policy.default_target_caps
    )
    assert hasattr(granted, "window_id")


async def test_quiet_window_releases_with_live_meter(tmp_path):
    if not await image_available(SIDECAR_IMG):
        pytest.skip("sidecar image not built")
    if not await image_available(ROOM_IMG):
        pytest.skip("room image missing")
    from redteam.control.network_ledger import SQLiteNetworkGrantLedger

    tag = uuid.uuid4().hex[:8]
    policy = ShellWorkroomPolicy(
        image=ROOM_IMG,
        allow_unpinned_image=True,
        default_timeout_seconds=20.0,
        max_timeout_seconds=60.0,
    )
    manager = ShellWorkroomManager(policy)
    cap = NetworkCapacity(max_flow_init_deliveries=4, max_output_bytes=4096)
    budget = NetworkBudget(global_capacity=cap, target_capacity=cap, install_capacity=cap)
    target_caps = WindowCaps(
        max_flow_init_deliveries=4,
        max_output_bytes=4096,
        delivery_rate_per_second=None,
        duration_seconds=300.0,
        purpose=NetworkPurpose.TARGET,
    )
    ledger = SQLiteNetworkGrantLedger(
        str(tmp_path / "quiet.db"), engagement_id="equiet", budget=budget
    )
    await ledger.setup()
    run_id = "gq" + tag
    instance_id = "i1"
    key = workroom_key_for(run_id, instance_id)
    network_policy = GatedNetworkPolicy(
        budget=budget,
        default_target_caps=target_caps,
        default_install_caps=WindowCaps(
            max_flow_init_deliveries=1,
            max_output_bytes=1024,
            delivery_rate_per_second=None,
            duration_seconds=60.0,
            purpose=NetworkPurpose.INSTALL,
        ),
        registry=PinnedRegistry(targets={}, mirrors={}),
    )

    async def run_docker(argv, stdin_bytes, timeout_seconds, output_limit=None):
        code, out, err, _t, _c = await manager._run_docker(
            argv, stdin_bytes, timeout_seconds, output_limit
        )
        return code, out, err, _t, _c

    ext = GatedWindowExtension(
        manager=manager, ledger=ledger, run_docker=run_docker, policy=network_policy
    )
    try:
        grant = await ext.open_window(
            run_id=run_id,
            instance_id=instance_id,
            epoch=1,
            caps=target_caps,
            allowed=(),
            holder_action_id="act_quiet_001",
            holder_invocation_id="inv_quiet_001",
        )
        assert grant.reserved_bytes == 4096 and grant.reserved_deliveries == 4
        probe_window = ext._open.get(key)
        assert probe_window is not None
        probe_ruleset = await ext._ruleset_text(probe_window.container_tag)
        with open("data/t005-gated/" + tag + "-quiet-open.txt", "w") as fh:
            fh.write(probe_ruleset)
        probe_descendants = await ext._no_descendants(probe_window)
        with open("data/t005-gated/" + tag + "-quiet-desc.txt", "w") as fh:
            fh.write("descendants-clear=" + repr(probe_descendants))
        settlement = await ext.close_window(key)
        with open("data/t005-gated/" + tag + "-quiet-settlement.txt", "w") as fh:
            fh.write(settlement.model_dump_json())
        assert settlement.state is WindowState.RELEASED
        assert settlement.outcome == "closed_verified"
        assert settlement.charged_bytes == 0 and settlement.charged_deliveries == 0
        assert settlement.released_bytes == 4096 and settlement.released_deliveries == 4
        assert settlement.metered_bytes == 0 and settlement.metered_packets == 0
        companion = ext.companion_for(grant.window_id)
        assert companion is not None
    finally:
        await _cleanup(container_name_for(key))


async def _room(tag):
    name = "t005-gated-room-" + tag
    code, _, _ = await docker(
        "create",
        "--name",
        name,
        "--network",
        "none",
        "--cap-drop",
        "ALL",
        "--label",
        "t005-gated",
        ROOM_IMG,
        "sleep",
        "infinity",
    )
    assert code == 0
    code, _, _ = await docker("start", name)
    assert code == 0
    return name


async def _sidecar(tag, room):
    name = "t005-gated-side-" + tag
    code, _, err = await docker(
        "create",
        "--name",
        name,
        "--network",
        "container:" + room,
        "--cap-add",
        "NET_ADMIN",
        "--cap-drop",
        "ALL",
        "--label",
        "t005-gated",
        SIDECAR_IMG,
    )
    assert code == 0, err.decode()
    code, _, err = await docker("start", name)
    assert code == 0, err.decode()
    return name


async def _mock(tag, net, prober_src):
    name = "t005-gated-mock-" + tag
    code, _, _ = await docker(
        "create",
        "--name",
        name,
        "--network",
        net,
        "--cap-drop",
        "ALL",
        "--label",
        "t005-gated",
        ROOM_IMG,
        "sleep",
        "infinity",
    )
    assert code == 0
    code, _, _ = await docker("start", name)
    assert code == 0
    await docker("cp", prober_src, name + ":/tmp/mock.py")
    await docker(
        "exec", "-d", name, "sh", "-c", "nohup python3 /tmp/mock.py > /tmp/mock-stdout.log 2>&1 &"
    )
    await asyncio.sleep(3)
    code, out, _ = await docker(
        "inspect", name, "--format", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}"
    )
    ip = out.decode().strip()
    assert ip
    return name, ip


async def _save(tag, label, container, remote):
    code, out, _ = await docker("exec", container, "cat", remote)
    if code == 0:
        path = "data/t005-gated/" + tag + "-" + label + ".txt"
        with open(path, "wb") as fh:
            fh.write(out)
    return code == 0


async def _cleanup(*names):
    for name in names:
        await docker("rm", "-f", name)


async def test_kernel_boundary(tmp_path):
    _ = tmp_path
    if not await image_available(SIDECAR_IMG):
        pytest.skip("sidecar image not built")
    if not await image_available(ROOM_IMG):
        pytest.skip("room image missing")
    os.makedirs("data/t005-gated", exist_ok=True)
    from redteam.capability.gated_window import AllowedFlow, build_ruleset

    tag = uuid.uuid4().hex[:8]
    net = "t005-gated-net-" + tag
    room = None
    side = None
    mock = None
    try:
        code, _, _ = await docker(
            "network", "create", "--driver", "bridge", "--internal", "--label", "t005-gated", net
        )
        assert code == 0
        room = await _room(tag)
        side = await _sidecar(tag, room)
        mock, mock_ip = await _mock(tag, net, "data/t005-gated/mock.py")
        rules = build_ruleset(
            queue_num=7,
            allowed=(AllowedFlow(ip=mock_ip, port=18080, proto="tcp"),),
            quota_bytes=20000,
        )
        code, _, err = await docker("exec", "-i", side, "nft", "-f", "-", stdin=rules.encode())
        assert code == 0, err.decode()[:300]
        code, out, _ = await docker("exec", side, "nft", "list", "ruleset")
        dumped = out.decode()
        with open("data/t005-gated/" + tag + "-ruleset.txt", "w") as fh:
            fh.write(dumped)
        assert "queue to 7" in dumped or "queue num 7" in dumped
        assert "denied_dns_local" in dumped and "denied_dns_ext" in dumped
        assert "denied_v6" in dumped and "denied_other" in dumped
        code, _, _ = await docker("network", "disconnect", "none", room)
        assert code == 0
        code, _, _ = await docker("network", "connect", net, room)
        assert code == 0
        await docker("cp", "data/t005-gated/prober.py", room + ":/tmp/prober.py")
        await docker(
            "exec",
            "--user",
            "nobody",
            "-d",
            room,
            "sh",
            "-c",
            "nohup python3 /tmp/prober.py tcp "
            + mock_ip
            + " 18080 40 1024 /tmp/probe-tcp.log >/dev/null 2>&1 &",
        )
        await docker(
            "exec",
            "--user",
            "nobody",
            "-d",
            room,
            "sh",
            "-c",
            "nohup python3 /tmp/prober.py udp "
            + mock_ip
            + " 18081 10 256 /tmp/probe-udp.log >/dev/null 2>&1 &",
        )
        await docker(
            "exec",
            "--user",
            "nobody",
            "-d",
            room,
            "sh",
            "-c",
            "nohup python3 /tmp/prober.py udp "
            + mock_ip
            + " 53 5 32 /tmp/probe-dns.log >/dev/null 2>&1 &",
        )
        await docker(
            "exec",
            "--user",
            "nobody",
            "-d",
            room,
            "sh",
            "-c",
            "nohup python3 /tmp/prober.py tcp 127.0.0.11 53 3 32 /tmp/dnsl.log >/dev/null 2>&1 &",
        )
        await docker(
            "exec",
            "--user",
            "nobody",
            "-d",
            room,
            "sh",
            "-c",
            "nohup python3 /tmp/prober.py tcp ::1 18099 3 32 /tmp/probe-v6.log >/dev/null 2>&1 &",
        )
        await docker(
            "exec",
            "-d",
            side,
            "sh",
            "-c",
            "timeout 40 tcpdump -i any -w /tmp/dns.pcap udp port 53 or tcp port 53 >/dev/null 2>&1",
        )
        await asyncio.sleep(30)
        code, out, _ = await docker("exec", side, "nft", "list", "ruleset")
        with open("data/t005-gated/" + tag + "-ruleset-end.txt", "w") as fh:
            fh.write(out.decode())
        counters = parse_named_counters(out.decode())
        with open("data/t005-gated/" + tag + "-counters.txt", "w") as fh:
            fh.write(repr(sorted((k, v.packets, v.bytes) for k, v in counters.items())))
        await _save(tag, "probe-tcp", room, "/tmp/probe-tcp.log")
        await _save(tag, "probe-udp", room, "/tmp/probe-udp.log")
        await _save(tag, "probe-dns", room, "/tmp/probe-dns.log")
        await _save(tag, "probe-v6", room, "/tmp/probe-v6.log")
        await _save(tag, "mock-tcp", mock, "/tmp/mock-tcp.log")
        await _save(tag, "mock-dns-tcp", mock, "/tmp/mock-dns-tcp.log")
        await _save(tag, "mock-dns-udp", mock, "/tmp/mock-dns-udp.log")
        basis = "data/t005-gated/" + tag + "-"
        tcp_log = open(basis + "mock-tcp.txt").read()
        dns_tcp_log = open(basis + "mock-dns-tcp.txt").read()
        dns_udp_log = open(basis + "mock-dns-udp.txt").read()
        # No verdict source is bound here, so every queued flow-init must die
        # in the kernel (no-listener drop): the mock hears nothing and no
        # allow-counter moves. The allowed-path positive runs in the window
        # e2e below with a live daemon plus ledger admission.
        assert "ACCEPT" not in tcp_log
        assert "ACCEPT" not in dns_tcp_log and "DATAGRAM" not in dns_udp_log
        assert counters["denied_dns_ext"].packets >= 5
        assert counters["denied_v6"].packets >= 1
        code, out, _ = await docker("exec", room, "cat", "/tmp/probe-tcp.log")
        tries = out.decode()
        assert " OK" not in tries
        assert "FAIL" in tries
        code, out, _ = await docker("exec", mock, "sh", "-c", "grep -c DATA /tmp/mock-tcp.log")
        assert out.decode().strip() == "0"
        met = counters["met_out"]
        assert met.bytes == 0 and met.packets == 0
        # No packet capture here: the NET_ADMIN-only sidecar intentionally
        # lacks NET_RAW, so tcpdump cannot run in it. DNS denial is proven
        # by the named deny counters plus the failing probes above.
    finally:
        await _cleanup(*[n for n in (room, side, mock) if n])
        if net:
            await docker("network", "rm", net)
        code, out, _ = await docker(
            "ps", "-a", "--filter", "name=t005-gated-" + tag, "--format", "{{.Names}}"
        )
        assert out.decode().strip() == ""


async def test_window_e2e_busy_static_settle(tmp_path):
    if not await image_available(SIDECAR_IMG):
        pytest.skip("sidecar image not built")
    if not await image_available(ROOM_IMG):
        pytest.skip("room image missing")
    os.makedirs("data/t005-gated", exist_ok=True)
    from redteam.capability.shell_workroom import ShellWorkroomError
    from redteam.control.network_ledger import SQLiteNetworkGrantLedger

    tag = uuid.uuid4().hex[:8]
    mock = None
    policy = ShellWorkroomPolicy(
        image=ROOM_IMG,
        allow_unpinned_image=True,
        default_timeout_seconds=20.0,
        max_timeout_seconds=60.0,
    )
    manager = ShellWorkroomManager(policy)
    pip_calls = []
    orig_run = manager._run_docker

    async def counting(argv, stdin_bytes, timeout_seconds, output_limit=None):
        if "pip" in argv:
            pip_calls.append(argv)
        return await orig_run(argv, stdin_bytes, timeout_seconds, output_limit)

    manager._run_docker = counting
    run_id = "gw" + tag
    instance_id = "i1"
    key = workroom_key_for(run_id, instance_id)
    grant = None
    ext = None
    try:
        from redteam.capability.gated_window import (
            GATED_LABEL,
            OPAQUE_PREFIX,
            opaque_window_tag,
        )

        wtag = opaque_window_tag(run_id, instance_id, 1)
        wnet = wtag.replace(OPAQUE_PREFIX, "gwn-", 1)
        await manager.ensure_workroom(key)
        code, _, err = await docker(
            "network", "create", "--driver", "bridge", "--internal",
            "--label", GATED_LABEL, wnet,
        )
        assert code == 0, err.decode(errors="replace")[:200]
        result = await manager.exec_script(
            key,
            "bash",
            "d=/workroom/.local/bin;mkdir -p $d&&printf ok>$d/tool1&&chmod +x $d/tool1",
        )
        assert result.exit_code == 0
        mock, mock_ip = await _mock(tag, wnet, "data/t005-gated/mock.py")
        policy_net = make_policy(mock_ip)
        ledger = SQLiteNetworkGrantLedger(
            str(tmp_path / "net.db"), engagement_id="egated", budget=policy_net.budget
        )
        await ledger.setup()

        async def run_docker(argv, stdin_bytes, timeout_seconds, output_limit=None):
            code, out, err, _t, _c = await counting(
                argv, stdin_bytes, timeout_seconds, output_limit
            )
            return code, out, err, _t, _c

        ext = GatedWindowExtension(
            manager=manager, ledger=ledger, run_docker=run_docker, policy=policy_net
        )
        manager.set_execution_admission(ext)
        grant = await ext.open_window(
            run_id=run_id,
            instance_id=instance_id,
            epoch=1,
            caps=policy_net.default_target_caps,
            allowed=(AllowedFlow(ip=mock_ip, port=18080, proto="tcp"),),
            holder_action_id="act_e2e_001",
            holder_invocation_id="inv_e2e_001",
        )
        assert grant.epoch == 1
        from redteam.capability.gated_window import opaque_window_tag

        room_name = container_name_for(key)
        known = manager._known.get(key)
        if known is not None:
            room_name = known
            # NOTE: /tmp is a tmpfs mount, so docker-cp files land underneath
            # it and stay invisible inside. The workroom surface persists.
            code, _, _ = await docker(
                "cp", "data/t005-gated/prober.py", room_name + ":/workroom/e2e-prober.py"
            )
            assert code == 0
        await asyncio.sleep(8)
        code, out, err = await docker(
            "exec",
            room_name,
            "python3",
                "/workroom/e2e-prober.py",
            "tcp",
            mock_ip,
            "18080",
            "3",
            "64",
                "/workroom/e2e-tcp.log",
        )
        with open("data/t005-gated/" + tag + "-e2e-prober.txt", "w") as fh:
            fh.write("exit=" + str(code) + " " + err.decode(errors="replace"))
        assert code == 0, err.decode(errors="replace")[:300]
        window = ext._open.get(key)
        assert window is not None
        verdicts = [entry.verdict for entry in window.admissions]
        with open("data/t005-gated/" + tag + "-e2e-verdicts.txt", "w") as fh:
            rpc_done = window.rpc_task.done() if window.rpc_task is not None else None
            fh.write(
                "verdicts=" + repr(sorted(verdicts)) + " rpc_error="
                + repr(window.rpc_error) + " rpc_done=" + repr(rpc_done)
            )
        code, out, _ = await docker("exec", room_name, "cat", "/workroom/e2e-tcp.log")
        with open("data/t005-gated/" + tag + "-e2e-tcp.txt", "w") as fh:
            fh.write(out.decode(errors="replace"))
        code, out, _ = await docker("exec", room_name, "ip", "route")
        with open("data/t005-gated/" + tag + "-e2e-route.txt", "w") as fh:
            fh.write(out.decode(errors="replace"))
        code, out, _ = await docker("exec", mock, "cat", "/tmp/mock-tcp.log")
        with open("data/t005-gated/" + tag + "-e2e-mock.txt", "w") as fh:
            fh.write(out.decode(errors="replace"))
            sidecar = opaque_window_tag(run_id, instance_id, 1)
            code, out, _ = await docker("exec", sidecar, "nft", "list", "ruleset")
            with open("data/t005-gated/" + tag + "-e2e-end.txt", "w") as fh:
                fh.write(out.decode(errors="replace"))
        assert "committed" in verdicts
        code, out, _ = await docker(
            "exec", mock, "sh", "-c", "grep -c ACCEPT /tmp/mock-tcp.log"
        )
        assert code == 0 and int(out.decode().strip()) >= 1
        assert ext.is_transmit_open(key)
        try:
            await manager.exec_script(key, "bash", "echo nope")
            raise AssertionError("busy must refuse")
        except ShellWorkroomError as exc:
            assert "window_busy" in str(exc)
        pip_calls.clear()
        records = await manager.list_instance_programs(run_id=run_id, instance_id=instance_id)
        names = [rec.name for rec in records]
        assert "tool1" in names
        assert all(rec.version is None for rec in records)
        assert not [c for c in pip_calls if "pip" in c]
        state = await manager.workroom_container_state(key)
        assert state == "running"
        settlement = await ext.close_window(key)
        with open("data/t005-gated/" + tag + "-settlement.txt", "w") as fh:
            fh.write(settlement.model_dump_json())
        if settlement.state is WindowState.RELEASED:
            assert settlement.outcome == "closed_verified"
        else:
            assert settlement.state is WindowState.QUARANTINED
            assert settlement.charged_bytes == grant.reserved_bytes
            assert settlement.outcome in ("unknown_fully_charged", "integrity_quarantined")

        instance_id2 = "i2"
        key2 = workroom_key_for(run_id, instance_id2)
        grant2 = None
        try:
            grant2 = await ext.open_window(
                run_id=run_id,
                instance_id=instance_id2,
                epoch=1,
                caps=policy_net.default_target_caps,
                allowed=(),
                holder_action_id="act_e2e_002",
                holder_invocation_id="inv_e2e_002",
            )
            assert grant2.epoch == 1
            await docker("rm", "-f", opaque_window_tag(run_id, instance_id2, 1))
            settlement2 = await ext.close_window(key2)
            assert settlement2.state is WindowState.QUARANTINED
            assert settlement2.charged_bytes == grant2.reserved_bytes
            assert settlement2.outcome == "unknown_fully_charged"
            assert await ext.release_quarantine(key2) is False
            with open("data/t005-gated/" + tag + "-settlement2.txt", "w") as fh:
                fh.write(settlement2.model_dump_json())
        finally:
            leftovers = [
                container_name_for(key),
                container_name_for(key2),
                opaque_window_tag(run_id, instance_id, 1),
                opaque_window_tag(run_id, instance_id2, 1),
                mock,
            ]
            await _cleanup(*[n for n in leftovers if n])
    finally:
        await docker("network", "rm", wnet)
        code, out, _ = await docker(
            "ps", "-a", "--filter", "name=t005-gated-" + tag, "--format", "{{.Names}}"
        )
        assert out.decode().strip() == ""
