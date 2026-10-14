"""Direct route identities and lifecycle guards with explicit injected Docker replies."""

import asyncio
import json
from functools import partial
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from redteam.capability.gated_network_policy import GatedNetworkPolicy
from redteam.capability.gated_routing import TargetRouteError, validate_target_route
from redteam.capability.gated_strategy import GatedExecutionPolicy, RetainedWindow
from redteam.capability.gated_window import AllowedFlow, GatedNetworkError, GatedWindowExtension
from redteam.capability.shell_workroom import ExecutionAdmission, ShellWorkroomError
from redteam.control.network_ledger import SQLiteNetworkGrantLedger
from redteam.core.contracts.engagement import EngagementScope
from redteam.core.contracts.network import GrantRefused, WindowState
from redteam.core.contracts.target import EndpointTarget
from redteam.core.scope_rules import check_target_scope
from tests.unit.test_gated_strategy_execute import (
    _FakeEvidence,
    _FakeExtension,
    _invocation,
    _ok_result,
    _parsed,
)
from tests.unit.test_gated_window import _policy, _stub_manager
from tests.unit.test_shell_adapter import _adopt_record, _FakeDocker, _manager

NET = "a" * 64
CONTAINER = "b" * 64
IP = "10.0.0.9"


def route_policy():
    return _policy().model_copy(update={"target_network_id": NET, "target_ports": {"t": (8080,)}})


def docker_records():
    network = {
        "Id": NET,
        "Name": "operator-targets",
        "Driver": "bridge",
        "Internal": True,
        "Containers": {CONTAINER: {"IPv4Address": IP + "/24"}},
    }
    container = {
        "Id": CONTAINER,
        "Image": "sha256:" + "c" * 64,
        "State": {"Running": True},
        "NetworkSettings": {"Networks": {"operator-targets": {"NetworkID": NET, "IPAddress": IP}}},
    }
    return network, container


def route_runner(network, container):
    async def runner(argv, stdin, timeout, limit=None):
        assert argv[:2] in (("network", "inspect"), ("container", "inspect"))
        assert argv[-1] == (NET if argv[0] == "network" else CONTAINER)
        assert "--format" in argv and ".Env" not in argv[3]
        record = network if argv[0] == "network" else container
        return 0, json.dumps(record).encode(), b"", False, False

    return runner


@pytest.mark.parametrize("ip", ["127.0.0.1", "127.0.0.2", "::1", "0.0.0.0", "169.254.2.3"])
async def test_workroom_cannot_claim_host_or_vm_loopback(ip):
    policy = _policy().model_copy(
        update={"registry": _policy().registry.model_copy(update={"targets": {"t": (ip,)}})}
    )
    runner = AsyncMock(side_effect=AssertionError("must not contact Docker or the target"))
    with pytest.raises(TargetRouteError):
        await validate_target_route(policy, runner)
    runner.assert_not_awaited()


async def test_container_route_binds_actual_network_container_image_and_ip():
    network, container = docker_records()
    runner = route_runner(network, container)
    result = await validate_target_route(route_policy(), runner)
    assert result["network_id"] == NET and result["connection"] == "operator_bridge"
    assert result["containers"][IP]["container_id"] == CONTAINER
    assert await validate_target_route(route_policy(), runner, expected=result) == result
    container["Image"] = "sha256:" + "d" * 64
    with pytest.raises(TargetRouteError, match="gated_target_route_changed"):
        await validate_target_route(route_policy(), runner, expected=result)


@pytest.mark.parametrize(
    "change,code",
    [
        ("network", "gated_target_network_mismatch"),
        ("pin", "gated_target_container_pin_mismatch"),
        ("container", "gated_target_container_identity_mismatch"),
        ("stopped", "gated_target_container_identity_mismatch"),
        ("malformed", "gated_target_route_unverifiable"),
    ],
)
async def test_operator_route_is_verified_instead_of_bypassing_dns_pins(change, code):
    network, container = docker_records()
    if change == "network":
        network["Id"] = "f" * 64
    elif change == "pin":
        network["Containers"][CONTAINER]["IPv4Address"] = "10.0.0.10/24"
    elif change == "container":
        container["Id"] = "f" * 64
    elif change == "stopped":
        container["State"]["Running"] = False
    else:
        container["NetworkSettings"] = None
    with pytest.raises(TargetRouteError, match=code):
        await validate_target_route(route_policy(), route_runner(network, container))


@pytest.mark.parametrize(
    "change,code",
    [
        ({"approved_port": 9090}, "gated_approved_target_mismatch"),
        ({"scope_ports": [9090]}, "gated_scope_denied"),
        ({"denied_ip": True}, "gated_pinned_denied"),
        ({"policy_ports": [9090]}, "gated_target_port_unselected"),
    ],
)
async def test_logical_target_physical_pin_and_port_must_all_match(change, code):
    ext = _FakeExtension(route_policy())
    if "policy_ports" in change:
        ext.policy = ext.policy.model_copy(
            update={"target_ports": {"t": tuple(change["policy_ports"])}}
        )
    scope = EngagementScope(
        allowed_hosts=[f"t:{p}" for p in change.get("scope_ports", [8080])],
        denied_hosts=[IP] if change.get("denied_ip") else [],
    )
    policy = GatedExecutionPolicy(ext, scope_validator=partial(check_target_scope, scope=scope))
    invocation = _invocation("shell_target_run")
    if "approved_port" in change:
        invocation = invocation.model_copy(
            update={
                "execution_context": invocation.execution_context.model_copy(
                    update={
                        "approved_target": EndpointTarget(host="t", port=change["approved_port"])
                    }
                )
            }
        )
    result = await policy.execute(
        invocation, _parsed(), AsyncMock(side_effect=AssertionError("no execution"))
    )
    assert result.error.code == code and ext.opened == 0


async def test_context_endpoint_change_refuses_before_grant_reservation():
    ledger = AsyncMock()
    ext = GatedWindowExtension(
        _stub_manager(),
        ledger,
        AsyncMock(return_value=(0, b"ssh://different", b"", False, False)),
        policy=_policy(),
        docker_endpoint={"context": "selected", "host": "unix:///original"},
    )
    with pytest.raises(GatedNetworkError, match="gated_docker_endpoint_changed"):
        await ext.open_window(
            run_id="r",
            instance_id="i",
            epoch=1,
            caps=_policy().default_target_caps,
            holder_action_id="a",
            holder_invocation_id="v",
        )
    ledger.request_window.assert_not_awaited()


async def test_shared_operator_bridge_detaches_without_being_deleted(tmp_path, monkeypatch):
    policy = route_policy()
    network, container = docker_records()
    lookup = route_runner(network, container)
    calls = []

    async def runner(argv, stdin, timeout, limit=None):
        calls.append(argv)
        if argv[:2] in (("network", "inspect"), ("container", "inspect")):
            return await lookup(argv, stdin, timeout, limit)
        if argv[:2] == ("image", "inspect"):
            return 0, policy.sidecar_image.encode(), b"", False, False
        return 0, b"", b"", False, False

    ledger = SQLiteNetworkGrantLedger(tmp_path / "net.db", engagement_id="e", budget=policy.budget)
    await ledger.setup()
    manager = _stub_manager()
    monkeypatch.setattr(manager, "ensure_workroom", AsyncMock(return_value="owned-room"))
    ext = GatedWindowExtension(
        manager,
        ledger,
        runner,
        policy=policy,
        target_route_identity=await validate_target_route(policy, lookup),
    )
    for name in (
        "_create_sidecar",
        "_apply_ruleset",
        "_add_live_elements",
        "_verify_open_posture_or_raise",
        "_rpc_loop",
    ):
        monkeypatch.setattr(ext, name, AsyncMock())
    await ext.open_window(
        run_id="r",
        instance_id="i",
        epoch=1,
        caps=policy.default_target_caps,
        allowed=(AllowedFlow(IP, 8080, "tcp"),),
        holder_action_id="a",
        holder_invocation_id="v",
    )
    window = ext._open["r.i"]
    assert ("network", "connect", NET, "owned-room") in calls
    assert ext.expected_network_ids("r.i") == {"operator-targets": NET}
    monkeypatch.setattr(ext, "_container_present", AsyncMock(return_value=True))
    await ext._teardown_network(window)
    assert ("network", "disconnect", NET, "owned-room") in calls
    assert not any(call[:2] in (("network", "create"), ("network", "rm")) for call in calls)
    if window.rpc_task:
        await window.rpc_task


async def test_stop_during_open_preserves_original_unknown_reservation(tmp_path, monkeypatch):
    policy = _policy()
    ledger = SQLiteNetworkGrantLedger(tmp_path / "net.db", engagement_id="e", budget=policy.budget)
    await ledger.setup()
    manager = _stub_manager()
    monkeypatch.setattr(manager, "ensure_workroom", AsyncMock(return_value="owned-room"))

    async def runner(argv, stdin, timeout, limit=None):
        return 0, policy.sidecar_image.encode(), b"", False, False

    ext = GatedWindowExtension(manager, ledger, runner, policy=policy)
    for name in (
        "_create_sidecar",
        "_apply_ruleset",
        "_add_live_elements",
        "_remove_sidecar",
        "_remove_owned_network",
    ):
        monkeypatch.setattr(ext, name, AsyncMock())
    monkeypatch.setattr(ext, "_transition_network", AsyncMock(side_effect=asyncio.CancelledError()))
    settle = AsyncMock(wraps=ledger.settle_window)
    monkeypatch.setattr(ledger, "settle_window", settle)
    with pytest.raises(asyncio.CancelledError):
        await ext.open_window(
            run_id="r",
            instance_id="i",
            epoch=1,
            caps=policy.default_target_caps,
            holder_action_id="a",
            holder_invocation_id="v",
        )
    assert not ext.is_transmit_open("r.i")
    settle.assert_awaited_once()
    original = await ledger.settle_window(**settle.await_args.kwargs)
    assert original.state is WindowState.QUARANTINED and original.released_bytes == 0
    assert isinstance(
        await ledger.request_window(
            run_id="r", instance_id="i", epoch=2, caps=policy.default_target_caps
        ),
        GrantRefused,
    )


@pytest.mark.parametrize("network_id", [NET, "d" * 64])
async def test_adoption_checks_network_id_even_when_the_name_matches(network_id):
    key = "run1.inst9"
    fake = _FakeDocker(containers={"unused": {"running": True}})
    from redteam.capability.shell_workroom import container_name_for

    name = container_name_for(key)
    fake.containers[name] = {"running": True}
    fake.adopt_record = _adopt_record(fake, key)
    fake.adopt_record["networks"] = {"operator-targets": {"NetworkID": network_id}}

    class Active(ExecutionAdmission):
        def expected_networks(self, key):
            return frozenset({"operator-targets"})

        def expected_network_ids(self, key):
            return {"operator-targets": NET}

    manager = _manager(fake)
    manager.set_execution_admission(Active())
    if network_id == NET:
        assert await manager.ensure_workroom(key) == name
    else:
        with pytest.raises(ShellWorkroomError, match="adoption_rejected"):
            await manager.ensure_workroom(key)


async def test_detached_settled_room_can_be_reused_only_without_data_networks():
    from redteam.capability.shell_workroom import container_name_for

    key = "run1.inst9"
    name = container_name_for(key)
    fake = _FakeDocker(containers={name: {"running": True}})
    fake.adopt_record = _adopt_record(fake, key)
    fake.adopt_record["networks"] = {}
    assert await _manager(fake).ensure_workroom(key) == name


async def test_route_identity_is_carried_by_retained_manifest_and_evidence():
    ext = _FakeExtension(route_policy())
    ext.target_route_identity = {"network_id": NET, "connection": "operator_bridge"}
    ext.docker_endpoint = {"context": "selected", "host": "ssh://operator-host"}
    evidence = _FakeEvidence()
    policy = GatedExecutionPolicy(
        ext, scope_validator=lambda target, **kwargs: (True, [], None), evidence_store=evidence
    )
    invocation = _invocation("shell_target_job_start")
    result = await policy.execute(
        invocation, _parsed(), AsyncMock(return_value=_ok_result("running"))
    )
    assert result.success
    retained = policy.retained_for("run_001.inst_001")
    assert (
        RetainedWindow.from_record(retained.to_record()).target_route_identity
        == ext.target_route_identity
    )
    await policy.close_retained(retained.key)
    assert evidence.records[0].metadata["target_route_identity"] == ext.target_route_identity


def test_route_defaults_preserve_legacy_policy_encoding_and_validate_ports():
    values = _policy().model_dump(mode="json")
    assert "target_network_id" not in values and "target_ports" not in values
    for ports in ({"t": [True]}, {"t": [0]}, {"foreign": [80]}):
        with pytest.raises(ValidationError):
            GatedNetworkPolicy.model_validate({**values, "target_ports": ports})


async def test_closed_reset_is_one_atomic_nft_transaction():
    from redteam.capability.gated_window import build_closed_ruleset

    runner = AsyncMock(return_value=(0, b"", b"", False, False))
    ext = GatedWindowExtension(_stub_manager(), AsyncMock(), runner, policy=_policy())
    await ext._apply_ruleset("sidecar", build_closed_ruleset(), atomic_reset=True)
    runner.assert_awaited_once()
    argv, rules, _, _ = runner.await_args.args
    assert argv == ("exec", "-i", "sidecar", "nft", "-f", "-")
    assert rules.startswith(b"delete table inet gated\ntable inet gated")
    assert b"policy drop" in rules
