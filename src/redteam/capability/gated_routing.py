"""Read-only validation of direct pins and an explicitly selected Docker bridge.

No address translation, tunnel, target request or scope authorization happens here.
Container identity is measured on the same daemon that owns the shell workroom.
"""

from __future__ import annotations

import ipaddress
import json
import re

from redteam.capability.gate_observer import RunDocker
from redteam.capability.gated_network_policy import GatedNetworkPolicy


class TargetRouteError(ValueError):
    pass


_NETWORK_FORMAT = (
    '{"Id":{{json .Id}},"Name":{{json .Name}},"Driver":{{json .Driver}},'
    '"Internal":{{json .Internal}},"Containers":{{json .Containers}}}'
)
_CONTAINER_FORMAT = (
    '{"Id":{{json .Id}},"Image":{{json .Image}},'
    '"State":{"Running":{{json .State.Running}}},'
    '"NetworkSettings":{"Networks":{{json .NetworkSettings.Networks}}}}'
)


def direct_target_ips(policy: GatedNetworkPolicy) -> tuple[str, ...]:
    ips = policy.registry.target_ips()
    for literal in ips:
        address = ipaddress.ip_address(literal)
        if address.version != 4:
            raise TargetRouteError("gated_ipv6_unsupported")
        if address.is_loopback:
            raise TargetRouteError("gated_target_loopback_ambiguous")
        if address.is_unspecified or address.is_multicast or address.is_link_local:
            raise TargetRouteError("gated_target_address_unsupported")
    return ips


async def _inspect(runner: RunDocker, *argv: str):
    # Inspect only route fields; never collect container environment/credentials.
    template = _NETWORK_FORMAT if argv[0] == "network" else _CONTAINER_FORMAT
    arguments = (*argv[:2], "--format", template, *argv[2:])
    code, out, _, timed_out, cut = await runner(arguments, None, 30, 1 << 20)
    if code or timed_out or cut:
        raise TargetRouteError("gated_target_route_unavailable")
    try:
        records = json.loads(out)
        if isinstance(records, dict):
            return records
        if not isinstance(records, list) or len(records) != 1 or not isinstance(records[0], dict):
            raise ValueError()
        return records[0]
    except (TypeError, ValueError):
        raise TargetRouteError("gated_target_route_unverifiable") from None


async def _validate_target_route(
    policy: GatedNetworkPolicy,
    runner: RunDocker,
    *,
    expected: dict | None = None,
) -> dict:
    ips = direct_target_ips(policy)
    identity: dict = {"connection": "daemon_routed", "target_ips": list(ips)}
    if policy.target_network_id is not None:
        network = await _inspect(runner, "network", "inspect", policy.target_network_id)
        if network.get("Id") != policy.target_network_id or network.get("Driver") != "bridge":
            raise TargetRouteError("gated_target_network_mismatch")
        members = network.get("Containers")
        if not isinstance(members, dict):
            raise TargetRouteError("gated_target_route_unverifiable")
        containers = {}
        for ip in ips:
            matches = [
                cid
                for cid, member in members.items()
                if isinstance(member, dict) and member.get("IPv4Address", "").split("/")[0] == ip
            ]
            if len(matches) != 1:
                raise TargetRouteError("gated_target_container_pin_mismatch")
            if re.fullmatch(r"[a-f0-9]{64}", matches[0]) is None:
                raise TargetRouteError("gated_target_route_unverifiable")
            container = await _inspect(runner, "container", "inspect", matches[0])
            attachment = (
                container.get("NetworkSettings", {})
                .get("Networks", {})
                .get(network.get("Name"), {})
            )
            if (
                container.get("Id") != matches[0]
                or container.get("State", {}).get("Running") is not True
                or not isinstance(container.get("Image"), str)
                or not container["Image"].startswith("sha256:")
                or attachment.get("NetworkID") != policy.target_network_id
                or attachment.get("IPAddress") != ip
            ):
                raise TargetRouteError("gated_target_container_identity_mismatch")
            containers[ip] = {"container_id": container["Id"], "image_id": container.get("Image")}
        identity = {
            **identity,
            "connection": "operator_bridge",
            "network_id": network["Id"],
            "network_name": network["Name"],
            "internal": network.get("Internal"),
            "containers": containers,
        }
    if expected is not None and identity != expected:
        raise TargetRouteError("gated_target_route_changed")
    return identity


async def validate_target_route(
    policy: GatedNetworkPolicy,
    runner: RunDocker,
    *,
    expected: dict | None = None,
) -> dict:
    try:
        return await _validate_target_route(policy, runner, expected=expected)
    except TargetRouteError:
        raise
    except (AttributeError, KeyError, TypeError, ValueError):
        raise TargetRouteError("gated_target_route_unverifiable") from None
