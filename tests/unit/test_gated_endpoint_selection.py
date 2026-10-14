"""Capability-owned gated preparation request, bounded presets and endpoint selection.

Pure fixtures only: no Docker, DNS, socket, subprocess or target request is made here.
"""

from __future__ import annotations

import asyncio
import inspect
import socket
import subprocess
from datetime import datetime, timezone
from typing import Any

import pytest
from pydantic import ValidationError

from redteam.capability.gated_manifest import (
    SHELL_INSTALL_RUN,
    SHELL_TARGET_JOB_START,
    SHELL_TARGET_RUN,
)
from redteam.capability.gated_network_policy import (
    SUPPORTED_GATED_PRESETS,
    EndpointRestriction,
    GatedLimits,
    GatedNetworkPolicy,
    GatedNetworkPolicyRequest,
    ManagedSidecarRequest,
    PinnedRegistry,
    SelectedEndpoint,
    build_gated_network_policy,
    policy_content_hash,
    resolve_managed_sidecar,
)
from redteam.capability.gated_strategy import GatedExecutionPolicy, GatedRoute
from redteam.core.contracts.network import GrantRefused, NetworkCapacity, NetworkPurpose
from redteam.core.contracts.target import DomainTarget, EndpointTarget
from redteam.core.contracts.tool import ExecutionContext, ToolInvocation
from redteam.core.ids import ActionId, AgentInstanceId, EngagementId, InvocationId, RunId, ToolId

PIN = "10.0.0.9"
IMAGE_ID = "sha256:" + "ab" * 32
SELECTED = {"t": [(443, "tcp")]}


def _built(**overrides):
    """A new prepare: the selection is always explicit unless a test overrides it."""
    kwargs = {
        "targets": {"t": (PIN,)},
        "sidecar": IMAGE_ID,
        "preset": "probe",
        "endpoints": SELECTED,
    }
    kwargs.update(overrides)
    return build_gated_network_policy(**kwargs)


def _loaded(*, targets=None, restriction=None):
    """A loaded legacy policy: the restriction stays optional and is never implied."""
    base = _built()
    return GatedNetworkPolicy(
        budget=base.budget,
        default_target_caps=base.default_target_caps,
        default_install_caps=base.default_install_caps,
        registry=PinnedRegistry(targets=targets or {"t": (PIN,)}, mirrors={}),
        endpoint_restriction=restriction,
        sidecar_image=IMAGE_ID,
    )


def _allow(target, *, denials_only: bool = False):
    return True, [], None


class _FakeExtension:
    """Stands in for the window extension. Any attempt to open a window fails the test."""

    def __init__(self, policy) -> None:
        self.policy = policy
        self.opened = 0

    async def open_window(self, **kwargs):
        self.opened += 1
        raise AssertionError("a window must not open for this refusal")

    async def close_window(self, key):
        raise AssertionError("no window exists")

    def companion_for(self, window_id):
        return None

    def is_transmit_open(self, key):
        return False


def _harness(policy, scope=_allow):
    extension = _FakeExtension(policy)
    return extension, GatedExecutionPolicy(extension, scope_validator=scope, evidence_store=None)


def _invocation(tool_name, *, target="t", port=8080, approved=None):
    context = ExecutionContext(
        engagement_id=EngagementId("eng_001"),
        run_id=RunId("run_001"),
        instance_id=AgentInstanceId("inst_001"),
        approved_target=approved or DomainTarget(domain="t"),
    )
    return ToolInvocation(
        invocation_id=InvocationId("inv_001"),
        action_id=ActionId("act_001"),
        run_id=RunId("run_001"),
        tool_id=ToolId("analysis." + tool_name),
        arguments={
            "language": "bash",
            "script": "true",
            "target_ref": target,
            "port": port,
        },
        execution_context=context,
        created_at=datetime.now(timezone.utc),
    )


def _parsed() -> dict:
    return {"language": "bash", "script": "true", "timeout_seconds": 5.0}


async def _never_runs():
    raise AssertionError("delegate must not run")


def _flows(route) -> list[tuple[str, int, str]]:
    return sorted((flow.ip, flow.port, flow.proto) for flow in route.allowed)


# Explicit values, presets and bounds


def test_preset_is_the_schema_owned_default_for_a_pinned_policy():
    policy = _built(preset="probe")
    assert isinstance(policy, GatedNetworkPolicy)
    probe = SUPPORTED_GATED_PRESETS["probe"]
    assert policy.budget.target_capacity == NetworkCapacity(
        max_flow_init_deliveries=probe.max_flow_init_deliveries,
        max_output_bytes=probe.max_output_bytes,
    )
    assert policy.default_target_caps.duration_seconds == probe.duration_seconds
    assert policy.default_target_caps.purpose is NetworkPurpose.TARGET
    assert set(SUPPORTED_GATED_PRESETS) == {"probe", "standard"}


def test_explicit_limits_produce_the_same_shape_as_the_matching_preset():
    probe = SUPPORTED_GATED_PRESETS["probe"]
    explicit = GatedLimits(
        max_flow_init_deliveries=probe.max_flow_init_deliveries,
        max_output_bytes=probe.max_output_bytes,
        duration_seconds=probe.duration_seconds,
    )
    assert _built(preset=None, limits=explicit).model_dump(mode="json") == (
        _built(preset="probe").model_dump(mode="json")
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {
            "preset": "probe",
            "limits": GatedLimits(
                max_flow_init_deliveries=8, max_output_bytes=4096, duration_seconds=60.0
            ),
        },
    ],
)
def test_exactly_one_of_preset_or_limits_is_required(kwargs):
    with pytest.raises(ValueError, match="gated_limits_selection_required"):
        _built(preset=kwargs.get("preset"), limits=kwargs.get("limits"))


def test_unsupported_preset_is_refused():
    with pytest.raises(ValueError, match="gated_preset_unsupported"):
        _built(preset="unlimited")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_flow_init_deliveries", 0),
        ("max_flow_init_deliveries", 100_001),
        ("max_flow_init_deliveries", True),
        ("max_output_bytes", 0),
        ("max_output_bytes", (1 << 30) + 1),
        ("duration_seconds", 0.0),
        ("duration_seconds", 3601.0),
        ("duration_seconds", float("inf")),
        ("delivery_rate_per_second", 0.0),
        ("delivery_rate_per_second", 1001.0),
    ],
)
def test_explicit_limits_outside_the_schema_bounds_are_refused(field, value):
    base = {"max_flow_init_deliveries": 8, "max_output_bytes": 4096, "duration_seconds": 60.0}
    base[field] = value
    with pytest.raises(ValidationError):
        GatedLimits(**base)


def test_explicit_limits_at_the_schema_maximum_are_accepted():
    limits = GatedLimits(
        max_flow_init_deliveries=100_000,
        max_output_bytes=1 << 30,
        duration_seconds=3600.0,
        delivery_rate_per_second=1000.0,
    )
    assert limits.max_flow_init_deliveries == 100_000


# Endpoint selection is required for every new prepare


@pytest.mark.parametrize("endpoints", [None, {}])
def test_new_prepare_refuses_a_missing_or_empty_endpoint_selection(endpoints):
    with pytest.raises(ValueError, match="gated_endpoint_selection_required"):
        _built(endpoints=endpoints)


def test_endpoint_selection_has_no_default_in_the_builder_signature():
    parameter = inspect.signature(build_gated_network_policy).parameters["endpoints"]
    assert parameter.default is inspect.Parameter.empty
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY


def test_every_pinned_target_needs_its_own_selection():
    with pytest.raises(ValueError, match="gated_endpoint_selection_missing_target"):
        _built(targets={"t": (PIN,), "u": ("10.0.0.10",)}, endpoints=SELECTED)


def test_restriction_keys_must_be_pinned_registry_targets():
    with pytest.raises(ValueError, match="endpoint_restriction_unknown_target"):
        _built(endpoints={"other": [(443, "tcp")]})


def test_new_request_type_requires_the_restriction():
    dumped = _built(sidecar=ManagedSidecarRequest()).model_dump(mode="json")
    assert dumped["endpoint_restriction"] is not None
    del dumped["endpoint_restriction"]
    with pytest.raises(ValidationError):
        GatedNetworkPolicyRequest.model_validate(dumped)


# Pins and sidecar selection


@pytest.mark.parametrize("pin", ["10.0.0.09", "::1", "10.0.0.9 ", "not-an-ip", "2001:db8::1"])
def test_pins_must_be_canonical_ipv4_literals(pin):
    with pytest.raises(ValueError, match="gated_pin_not_canonical_ipv4"):
        _built(targets={"t": (pin,)})


def test_pin_sets_and_names_are_required_and_exact():
    with pytest.raises(ValueError, match="gated_target_pins_required"):
        _built(targets={})
    with pytest.raises(ValueError, match="gated_target_pin_required"):
        _built(targets={"t": []})
    with pytest.raises(ValueError, match="gated_target_name_invalid"):
        _built(targets={" t": (PIN,)})


@pytest.mark.parametrize("image", ["mutable:latest", "t005-gate-sidecar", "", "sha256:abc"])
def test_unpinned_sidecar_is_refused_by_the_builder(image):
    with pytest.raises(ValueError, match="gated_sidecar_image_unpinned"):
        _built(sidecar=image)


def test_pinned_policy_without_sidecar_is_inspectable_but_never_opens_a_gate():
    request = _built(sidecar=ManagedSidecarRequest())
    inspect_only = GatedNetworkPolicy.model_validate(
        request.model_dump(mode="json", exclude={"managed_sidecar"})
    )
    assert inspect_only.sidecar_image is None
    assert inspect_only.registry.target_ips() == (PIN,)
    with pytest.raises(ValueError, match="gated_sidecar_image_required"):
        inspect_only.require_sidecar_image()


def test_unpinned_policy_value_is_inspectable_but_fails_the_gate_check():
    dumped = _built(sidecar=ManagedSidecarRequest()).model_dump(
        mode="json", exclude={"managed_sidecar"}
    )
    policy = GatedNetworkPolicy.model_validate({**dumped, "sidecar_image": "t005-gate-sidecar"})
    with pytest.raises(ValueError, match="gated_sidecar_image_unpinned"):
        policy.require_sidecar_image()


# Managed request: unresolved until preparation resolves an immutable image ID


def test_managed_request_is_unresolved_and_cannot_be_read_as_a_resolved_policy():
    request = _built(sidecar=ManagedSidecarRequest())
    assert isinstance(request, GatedNetworkPolicyRequest)
    with pytest.raises(ValueError, match="gated_sidecar_unresolved"):
        request.require_sidecar_image()
    dumped = request.model_dump(mode="json")
    assert "sidecar_image" not in dumped
    assert dumped["managed_sidecar"] == {"recipe": "gate-sidecar"}
    with pytest.raises(ValidationError):
        GatedNetworkPolicy.model_validate(dumped)


def test_resolution_copies_every_other_field_and_requires_an_immutable_image_id():
    request = _built(sidecar=ManagedSidecarRequest(), endpoints={"t": [(443, "tcp")]})
    resolved = resolve_managed_sidecar(request, IMAGE_ID)
    assert isinstance(resolved, GatedNetworkPolicy)
    assert resolved.require_sidecar_image() == IMAGE_ID
    assert resolved.model_dump(mode="json", exclude={"sidecar_image"}) == (
        request.model_dump(mode="json", exclude={"managed_sidecar"})
    )
    bad_images = ("sha256:" + "AB" * 32, "sha256:abc", "t005-gate-sidecar")
    for bad in (*bad_images, "repo@sha256:" + "cd" * 32):
        with pytest.raises(ValueError, match="gated_sidecar_resolution_requires_image_id"):
            resolve_managed_sidecar(request, bad)
    with pytest.raises(ValueError, match="gated_sidecar_resolution_requires_request"):
        resolve_managed_sidecar(resolved, IMAGE_ID)


async def test_unresolved_request_is_refused_before_any_window_opens():
    extension, strategy = _harness(_built(sidecar=ManagedSidecarRequest()))
    result = await strategy.execute(_invocation(SHELL_TARGET_RUN), _parsed(), _never_runs)
    assert result.success is False
    assert result.error.code == "gated_sidecar_unresolved"
    assert extension.opened == 0


# Disabled install route


def test_install_route_is_disabled_by_construction():
    policy = _built()
    assert policy.budget.install_capacity == NetworkCapacity(
        max_flow_init_deliveries=0, max_output_bytes=0
    )
    assert policy.registry.mirrors == {}
    assert policy.install_proxy is None
    assert policy.caps_for(NetworkPurpose.INSTALL).purpose is NetworkPurpose.INSTALL


def test_install_request_has_no_mirror_route_under_the_disabled_install_budget():
    _, strategy = _harness(_built())
    route = strategy.resolve(_invocation(SHELL_INSTALL_RUN, target="mirror"))
    assert isinstance(route, GrantRefused)
    assert route.code == "gated_unknown_mirror"


# Pure construction: no build, DNS, request or policy write


def test_builder_is_pure_no_build_dns_request_or_process(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("the pure builder must not touch the host or network")

    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    request = _built(sidecar=ManagedSidecarRequest(), endpoints={"t": [(443, "tcp")]})
    assert isinstance(request, GatedNetworkPolicyRequest)


# Endpoint selection: validation, canonical identity and immutability


@pytest.mark.parametrize(
    ("port", "transport"),
    [(0, "tcp"), (65536, "tcp"), (443, "icmp"), (True, "tcp")],
)
def test_selected_endpoint_port_and_transport_are_validated(port, transport):
    with pytest.raises(ValidationError):
        SelectedEndpoint(port=port, transport=transport)


def test_duplicate_and_empty_selections_are_refused():
    with pytest.raises(ValueError, match="endpoint_restriction_duplicate"):
        _built(endpoints={"t": [(443, "tcp"), (443, "tcp")]})
    with pytest.raises(ValueError, match="endpoint_restriction_target_empty"):
        _built(endpoints={"t": []})
    with pytest.raises(ValueError, match="endpoint_restriction_empty"):
        EndpointRestriction(selections={})


def test_restriction_is_canonical_and_its_hash_follows_the_selection():
    ordered = _built(endpoints={"t": [(8443, "tcp"), (443, "tcp")]})
    reversed_input = _built(endpoints={"t": [(443, "tcp"), (8443, "tcp")]})
    assert policy_content_hash(ordered) == policy_content_hash(reversed_input)
    assert policy_content_hash(ordered) != policy_content_hash(
        _built(endpoints={"t": [(443, "tcp")]})
    )
    assert policy_content_hash(ordered) != policy_content_hash(
        _built(endpoints={"t": [(443, "tcp"), (8443, "udp")]})
    )


def test_restriction_model_is_immutable():
    restriction = EndpointRestriction(
        selections={"t": (SelectedEndpoint(port=443, transport="tcp"),)}
    )
    with pytest.raises(ValidationError):
        restriction.selections = {}


# Old loaded policies: unchanged, never presented as a narrower selection


def test_legacy_loaded_policy_has_no_restriction_and_no_new_keys():
    legacy = _loaded()
    assert legacy.endpoint_restriction is None
    dumped = legacy.model_dump(mode="json")
    assert set(dumped) == {
        "budget",
        "default_target_caps",
        "default_install_caps",
        "registry",
        "install_proxy",
        "sidecar_image",
    }
    assert GatedNetworkPolicy.model_validate(dumped) == legacy


def test_legacy_loaded_policy_keeps_the_scope_bound_tcp_and_udp_flows():
    _, strategy = _harness(_loaded())
    route = strategy.resolve(_invocation(SHELL_TARGET_RUN, port=8080))
    assert isinstance(route, GatedRoute)
    assert _flows(route) == [(PIN, 8080, "tcp"), (PIN, 8080, "udp")]


# Invocation boundary: enforced before any window opens


async def test_in_scope_port_outside_the_selection_is_refused_before_open():
    extension, strategy = _harness(_built())
    result = await strategy.execute(
        _invocation(SHELL_TARGET_RUN, port=8080), _parsed(), _never_runs
    )
    assert result.success is False
    assert result.error.code == "gated_endpoint_not_selected"
    assert extension.opened == 0


def test_unselected_udp_is_not_generated_for_a_tcp_only_selection():
    _, strategy = _harness(_built())
    route = strategy.resolve(_invocation(SHELL_TARGET_RUN, port=443))
    assert isinstance(route, GatedRoute)
    assert _flows(route) == [(PIN, 443, "tcp")]


def test_a_selection_naming_both_transports_still_generates_both():
    _, strategy = _harness(_built(endpoints={"t": [(53, "udp"), (53, "tcp")]}))
    route = strategy.resolve(_invocation(SHELL_TARGET_RUN, port=53))
    assert isinstance(route, GatedRoute)
    assert _flows(route) == [(PIN, 53, "tcp"), (PIN, 53, "udp")]


def test_selected_but_out_of_scope_port_is_still_denied_by_the_original_authority():
    def scope(target, *, denials_only: bool = False):
        return target.port != 9999, [], None

    _, strategy = _harness(_built(endpoints={"t": [(9999, "tcp")]}), scope=scope)
    result = strategy.resolve(_invocation(SHELL_TARGET_RUN, port=9999))
    assert isinstance(result, GrantRefused)
    assert result.code == "gated_scope_denied"


def test_loaded_restricted_policy_refuses_a_registry_name_without_a_selection():
    restriction = EndpointRestriction(
        selections={"t": (SelectedEndpoint(port=443, transport="tcp"),)}
    )
    policy = _loaded(
        targets={"t": (PIN,), "u": ("10.0.0.10",)},
        restriction=restriction,
    )
    _, strategy = _harness(policy)
    result = strategy.resolve(
        _invocation(
            SHELL_TARGET_RUN,
            target="u",
            port=443,
            approved=DomainTarget(domain="u"),
        )
    )
    assert isinstance(result, GrantRefused)
    assert result.code == "gated_endpoint_not_selected"


async def test_window_open_refuses_an_unresolved_request_before_reserving_capacity():
    from redteam.capability.gated_window import AllowedFlow, GatedNetworkError, GatedWindowExtension

    class _Trap:
        def __getattr__(self, name):
            raise AssertionError("ledger or docker touched before the sidecar check: " + name)

    request = _built(sidecar=ManagedSidecarRequest())
    extension = GatedWindowExtension(
        manager=_Trap(), ledger=_Trap(), run_docker=_Trap(), policy=request
    )
    with pytest.raises(GatedNetworkError, match="gated_sidecar_unresolved"):
        await extension.open_window(
            run_id="run_001",
            instance_id="inst_001",
            epoch=1,
            caps=request.caps_for(NetworkPurpose.TARGET),
            allowed=(AllowedFlow(ip=PIN, port=443, proto="tcp"),),
            holder_action_id="act_001",
            holder_invocation_id="inv_001",
        )


# Read-only selection, no shared mapping, and refusals that persist after an attempted change


def test_selection_mapping_refuses_key_update_and_delete():
    restriction = EndpointRestriction(
        selections={"t": (SelectedEndpoint(port=443, transport="tcp"),)}
    )
    view: Any = restriction.selections
    with pytest.raises(TypeError):
        view["t"] = (SelectedEndpoint(port=8080, transport="udp"),)
    with pytest.raises(TypeError):
        view["u"] = (SelectedEndpoint(port=8080, transport="udp"),)
    with pytest.raises(TypeError):
        del view["t"]
    assert restriction.protocols_for("t", 443) == ("tcp",)
    assert restriction.protocols_for("t", 8080) is None


def test_external_input_mapping_mutation_cannot_change_the_restriction():
    raw = {"t": (SelectedEndpoint(port=443, transport="tcp"),)}
    restriction = EndpointRestriction(selections=raw)
    raw["t"] = (SelectedEndpoint(port=8080, transport="udp"),)
    raw["u"] = (SelectedEndpoint(port=8080, transport="udp"),)
    assert set(restriction.selections) == {"t"}
    assert restriction.protocols_for("t", 443) == ("tcp",)
    assert restriction.protocols_for("t", 8080) is None


def test_external_builder_input_mutation_cannot_change_the_policy():
    endpoints = {"t": [(443, "tcp")]}
    policy = _built(endpoints=endpoints)
    before = policy_content_hash(policy)
    endpoints["t"].append((8080, "udp"))
    endpoints["u"] = [(8080, "udp")]
    assert policy_content_hash(policy) == before
    assert policy.endpoint_restriction.protocols_for("t", 8080) is None


def test_stable_hash_after_an_attempted_restriction_change():
    policy = _built(endpoints={"t": [(443, "tcp")]})
    before = policy_content_hash(policy)
    view: Any = policy.endpoint_restriction.selections
    with pytest.raises(TypeError):
        view["t"] = (SelectedEndpoint(port=8080, transport="udp"),)
    with pytest.raises(TypeError):
        del view["t"]
    assert policy_content_hash(policy) == before
    assert before == policy_content_hash(_built(endpoints={"t": [(443, "tcp")]}))


def test_resolution_never_shares_the_restriction_or_registry_with_the_request():
    request = _built(sidecar=ManagedSidecarRequest(), endpoints={"t": [(443, "tcp")]})
    resolved = resolve_managed_sidecar(request, IMAGE_ID)
    assert resolved.endpoint_restriction is not request.endpoint_restriction
    assert resolved.registry is not request.registry
    request_before = request.model_dump(mode="json")
    resolved_before = resolved.model_dump(mode="json", exclude={"sidecar_image"})

    request_view: Any = request.endpoint_restriction.selections
    with pytest.raises(TypeError):
        request_view["t"] = (SelectedEndpoint(port=8080, transport="udp"),)
    resolved_view: Any = resolved.endpoint_restriction.selections
    with pytest.raises(TypeError):
        resolved_view["t"] = (SelectedEndpoint(port=8080, transport="udp"),)
    assert request.model_dump(mode="json") == request_before
    assert resolved.model_dump(mode="json", exclude={"sidecar_image"}) == resolved_before

    # PinnedRegistry still holds plain dicts, so the resolved policy must keep its own copy.
    request.registry.targets["u"] = ("10.0.0.10",)
    assert "u" in request.registry.targets
    assert "u" not in resolved.registry.targets


async def test_strategy_refusal_persists_after_an_attempted_selection_change():
    endpoints = {"t": [(443, "tcp")]}
    policy = _built(endpoints=endpoints)
    view: Any = policy.endpoint_restriction.selections
    with pytest.raises(TypeError):
        view["t"] = (SelectedEndpoint(port=8080, transport="tcp"),)
    endpoints["t"].append((8080, "tcp"))
    extension, strategy = _harness(policy)
    result = await strategy.execute(
        _invocation(SHELL_TARGET_RUN, port=8080), _parsed(), _never_runs
    )
    assert result.success is False
    assert result.error.code == "gated_endpoint_not_selected"
    assert extension.opened == 0
    route = strategy.resolve(_invocation(SHELL_TARGET_RUN, port=443))
    assert isinstance(route, GatedRoute)
    assert _flows(route) == [(PIN, 443, "tcp")]


@pytest.mark.parametrize(
    ("approved", "target", "port"),
    [
        (DomainTarget(domain="t.test"), "deployment-alias", 443),
        (EndpointTarget(host="t.test", port=443), "t.test", 8443),
    ],
)
async def test_approved_target_mismatch_refuses_before_window_or_ledger(approved, target, port):
    policy = _built(
        targets={"t.test": (PIN,), "deployment-alias": ("10.0.0.10",)},
        endpoints={"t.test": [(443, "tcp")], "deployment-alias": [(443, "tcp")]},
    )
    extension, strategy = _harness(policy)

    result = await strategy.execute(
        _invocation(
            SHELL_TARGET_RUN,
            target=target,
            port=port,
            approved=approved,
        ),
        _parsed(),
        _never_runs,
    )

    assert not result.success
    assert result.error.code == "gated_approved_target_mismatch"
    assert extension.opened == 0


def test_approved_logical_host_resolves_only_its_operator_registry_pins():
    policy = _built(
        targets={"t": (PIN,)},
        endpoints={"t": [(443, "tcp")]},
    )
    calls = []

    def scope(target, *, denials_only=False):
        calls.append((target.host, target.port, denials_only))
        return True, [], None

    _extension, strategy = _harness(policy, scope)
    route = strategy.resolve(
        _invocation(
            SHELL_TARGET_RUN,
            target="t",
            port=443,
            approved=DomainTarget(domain="T."),
        )
    )

    assert isinstance(route, GatedRoute)
    assert _flows(route) == [(PIN, 443, "tcp")]
    assert calls == [("t.", 443, False), (PIN, 443, True)]


def test_gated_descriptors_declare_target_ref_source_by_route_form():
    from redteam.capability.gated_manifest import SHELL_INSTALL_RUN, build_gated_descriptors

    sources = {
        item.tool_name: item.target_binding.target_ref_source
        for item in build_gated_descriptors()
    }
    assert sources[SHELL_TARGET_RUN] == "logical_host"
    assert sources[SHELL_TARGET_JOB_START] == "logical_host"
    assert sources[SHELL_INSTALL_RUN] == "mcp_ref"
