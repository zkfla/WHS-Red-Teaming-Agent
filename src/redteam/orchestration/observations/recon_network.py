"""Map bounded DNS, TLS, OIDC and port-scan observations into the graph."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from typing import Any

from redteam.attack_graph.identity import (
    endpoint_node_id,
    host_node_id,
    node_id_for_target,
    service_node_id,
)
from redteam.core.contracts.target import AnyTarget, HostTarget
from redteam.core.contracts.tool import ToolResult
from redteam.core.enums import GraphEdgeType, GraphNodeType
from redteam.orchestration.observations.mapper import EdgeMutation, GraphMutation, NodeMutation

_HEX_64 = re.compile(r"^[0-9a-fA-F]{64}$")
_SAFE_VALUE = re.compile(r"^[A-Za-z0-9_.:+/@-]{1,100}$")
_OIDC_CAPABILITIES = frozenset(
    {
        "response_types_supported",
        "grant_types_supported",
        "code_challenge_methods_supported",
        "token_endpoint_auth_methods_supported",
    }
)
_OIDC_ENDPOINTS = frozenset(
    {
        "authorization_endpoint",
        "token_endpoint",
        "jwks_uri",
        "revocation_endpoint",
    }
)
_OIDC_CAPABILITY_FACT_KEYS = {
    "response_types_supported": "response_types_supported",
    "grant_types_supported": "grant_types_supported",
    "code_challenge_methods_supported": "code_challenge_methods_supported",
    "token_endpoint_auth_methods_supported": "endpoint_auth_methods_supported",
}
_OIDC_ENDPOINT_FACT_KEYS = {
    "authorization_endpoint": "authorization",
    "token_endpoint": "credential_exchange",
    "jwks_uri": "jwks",
    "revocation_endpoint": "revocation",
}
_NMAP_STATES = frozenset(
    {
        "open",
        "closed",
        "filtered",
        "unfiltered",
        "open|filtered",
        "closed|filtered",
        "unknown",
    }
)


def _context_key(kind: str, *parts: object) -> str:
    encoded = json.dumps([kind, *parts], separators=(",", ":"), sort_keys=True).encode()
    return hashlib.sha256(encoded).hexdigest()[:24]


def _host_context(
    *, target: AnyTarget, arguments: dict[str, Any], result: ToolResult
) -> tuple[str, str, dict[str, Any]] | None:
    if (
        not result.success
        or not isinstance(result.output, dict)
        or not isinstance(target, HostTarget)
    ):
        return None
    target_ref = arguments.get("target_ref")
    if (
        not isinstance(target_ref, str)
        or not target_ref
        or result.output.get("target_ref") != target_ref
    ):
        return None
    return target.host, target_ref, result.output


def _safe_path(value: Any) -> str | None:
    if not isinstance(value, str) or not value.startswith("/") or len(value) > 512:
        return None
    if any(ord(char) < 32 or char in "?#%\\" for char in value):
        return None
    if "//" in value or any(part in {".", ".."} for part in value.split("/")):
        return None
    return value


def map_recon_dns_resolve(
    *, target: AnyTarget, arguments: dict[str, Any], result: ToolResult
) -> GraphMutation:
    context = _host_context(target=target, arguments=arguments, result=result)
    if context is None:
        return GraphMutation()
    host, target_ref, output = context
    addresses = output.get("addresses")
    if not isinstance(addresses, list) or not 1 <= len(addresses) <= 64:
        return GraphMutation()
    normalized: list[str] = []
    try:
        for value in addresses:
            if not isinstance(value, str):
                return GraphMutation()
            normalized.append(str(ipaddress.ip_address(value)))
    except ValueError:
        return GraphMutation()
    normalized = sorted(set(normalized))
    return GraphMutation(
        nodes=[
            NodeMutation(
                GraphNodeType.HOST,
                host_node_id(host),
                attrs={"dns_resolved": True},
                facts={"host": host, "target_ref": target_ref, "addresses": normalized},
                labels=["observed-by:recon.dns_resolve"],
                observations=[
                    {
                        "kind": "dns_resolution",
                        "context_key": _context_key("dns", host, normalized),
                        "target_ref": target_ref,
                        "addresses": normalized,
                    }
                ],
            )
        ]
    )


def map_recon_tls_inspect(
    *, target: AnyTarget, arguments: dict[str, Any], result: ToolResult
) -> GraphMutation:
    context = _host_context(target=target, arguments=arguments, result=result)
    if context is None:
        return GraphMutation()
    host, target_ref, output = context
    protocol = output.get("protocol")
    cipher = output.get("cipher")
    digest = output.get("certificate_sha256")
    if (
        output.get("verified") is not True
        or not isinstance(protocol, str)
        or _SAFE_VALUE.fullmatch(protocol) is None
        or not isinstance(cipher, str)
        or _SAFE_VALUE.fullmatch(cipher) is None
        or not isinstance(digest, str)
        or _HEX_64.fullmatch(digest) is None
    ):
        return GraphMutation()
    host_id = host_node_id(host)
    service_id = service_node_id("tls", host)
    observation = {
        "kind": "tls_handshake",
        "context_key": _context_key("tls", host, protocol, cipher, digest),
        "target_ref": target_ref,
        "verified": True,
        "protocol": protocol,
        "cipher": cipher,
        "certificate_sha256": f"sha256:{digest.lower()}",
    }
    return GraphMutation(
        nodes=[
            NodeMutation(
                GraphNodeType.SERVICE,
                service_id,
                attrs={"tls_verified": True},
                facts={
                    "host": host,
                    "target_ref": target_ref,
                    "service_name": "tls",
                    "protocol": protocol,
                    "cipher": cipher,
                    "certificate_sha256": f"sha256:{digest.lower()}",
                },
                labels=["observed-by:recon.tls_inspect"],
                observations=[observation],
            )
        ],
        edges=[EdgeMutation(GraphEdgeType.HOSTS, host_id, service_id)],
    )


def map_recon_oidc_inventory(
    *, target: AnyTarget, arguments: dict[str, Any], result: ToolResult
) -> GraphMutation:
    context = _host_context(target=target, arguments=arguments, result=result)
    path = _safe_path(arguments.get("path"))
    if context is None or path is None:
        return GraphMutation()
    host, target_ref, output = context
    capabilities = output.get("capabilities")
    endpoints_present = output.get("endpoints_present")
    if (
        not isinstance(capabilities, dict)
        or set(capabilities) != _OIDC_CAPABILITIES
        or not isinstance(endpoints_present, dict)
        or set(endpoints_present) != _OIDC_ENDPOINTS
        or any(type(value) is not bool for value in endpoints_present.values())
        or output.get("source_untrusted") is not True
        or output.get("endpoints_followed") is not False
        or output.get("verdict") != "observation_only"
    ):
        return GraphMutation()
    safe_capabilities: dict[str, list[str]] = {}
    for key, values in capabilities.items():
        if not isinstance(values, list) or len(values) > 30:
            return GraphMutation()
        if any(
            not isinstance(value, str) or _SAFE_VALUE.fullmatch(value) is None
            for value in values
        ):
            return GraphMutation()
        safe_capabilities[_OIDC_CAPABILITY_FACT_KEYS[key]] = list(dict.fromkeys(values))
    safe_endpoint_presence = {
        _OIDC_ENDPOINT_FACT_KEYS[key]: value for key, value in endpoints_present.items()
    }
    endpoint_id = endpoint_node_id("GET", path, host=host)
    observation = {
        "kind": "oidc_inventory",
        "context_key": _context_key(
            "oidc", host, path, safe_capabilities, safe_endpoint_presence
        ),
        "target_ref": target_ref,
        "path": path,
        "capabilities": safe_capabilities,
        "endpoint_presence": dict(sorted(safe_endpoint_presence.items())),
        "source_untrusted": True,
        "endpoints_followed": False,
        "verdict": "observation_only",
    }
    source_id = node_id_for_target(target)
    return GraphMutation(
        nodes=[
            NodeMutation(
                GraphNodeType.ENDPOINT,
                endpoint_id,
                attrs={"oidc_metadata_observed": True},
                facts={
                    "host": host,
                    "target_ref": target_ref,
                    "path": path,
                    "method": "GET",
                    "observed_target": path,
                    "source_untrusted": True,
                },
                labels=["observed-by:recon.oidc_inventory"],
                observations=[observation],
            )
        ],
        edges=[EdgeMutation(GraphEdgeType.OBSERVED, source_id, endpoint_id)],
    )


def map_recon_port_scan(
    *, target: AnyTarget, arguments: dict[str, Any], result: ToolResult
) -> GraphMutation:
    context = _host_context(target=target, arguments=arguments, result=result)
    if context is None:
        return GraphMutation()
    host, target_ref, output = context
    ports = output.get("ports")
    if (
        not isinstance(ports, list)
        or len(ports) > 16
        or type(output.get("engine_exit_success")) is not bool
        or output.get("verdict") != "observation_only"
    ):
        return GraphMutation()
    normalized: list[dict[str, Any]] = []
    seen_ports: set[int] = set()
    for item in ports:
        if not isinstance(item, dict):
            return GraphMutation()
        port = item.get("port")
        protocol = item.get("protocol")
        state = item.get("state")
        if (
            type(port) is not int
            or not 1 <= port <= 65535
            or protocol != "tcp"
            or state not in _NMAP_STATES
            or port in seen_ports
        ):
            return GraphMutation()
        seen_ports.add(port)
        normalized.append({"port": port, "protocol": protocol, "state": state})
    normalized.sort(key=lambda item: item["port"])
    host_id = host_node_id(host)
    nodes = [
        NodeMutation(
            GraphNodeType.HOST,
            host_id,
            attrs={"port_scan_complete": output["engine_exit_success"]},
            facts={"host": host, "target_ref": target_ref},
            labels=["observed-by:recon.port_scan"],
            observations=[
                {
                    "kind": "tcp_port_scan",
                    "context_key": _context_key("ports", host, normalized),
                    "target_ref": target_ref,
                    "ports": normalized,
                    "engine_exit_success": output["engine_exit_success"],
                    "verdict": "observation_only",
                }
            ],
        )
    ]
    edges: list[EdgeMutation] = []
    for item in normalized:
        if item["state"] != "open":
            continue
        service_id = service_node_id(f"tcp:{item['port']}", host)
        nodes.append(
            NodeMutation(
                GraphNodeType.SERVICE,
                service_id,
                attrs={"open": True},
                facts={
                    "host": host,
                    "target_ref": target_ref,
                    "port": item["port"],
                    "transport_protocol": "tcp",
                    "state": "open",
                },
                labels=["discovered-by:recon.port_scan"],
            )
        )
        edges.append(EdgeMutation(GraphEdgeType.HOSTS, host_id, service_id))
    return GraphMutation(nodes=nodes, edges=edges)
