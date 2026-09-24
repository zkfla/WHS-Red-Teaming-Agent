"""Graph mappings for the bundled network reconnaissance tools."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from redteam.core.contracts.target import HostTarget
from redteam.core.contracts.tool import ToolResult
from redteam.core.enums import GraphEdgeType, GraphNodeType
from redteam.core.ids import ActionId, InvocationId, ToolId
from redteam.orchestration.observations import (
    GraphMutation,
    default_result_mapper_registry,
)

NOW = datetime.now(timezone.utc)
TARGET = HostTarget(host="bank.local")


def _map(tool_id: str, arguments: dict, output: dict) -> GraphMutation:
    result = ToolResult(
        invocation_id=InvocationId("inv_recon"),
        action_id=ActionId("action_recon"),
        tool_id=ToolId(tool_id),
        success=True,
        output=output,
        started_at=NOW,
        finished_at=NOW,
    )
    return default_result_mapper_registry().map_result(
        tool_id=ToolId(tool_id),
        target=TARGET,
        arguments=arguments,
        result=result,
    )


def test_dns_resolution_updates_the_existing_host_without_minting_ip_hosts() -> None:
    mutation = _map(
        "recon.dns_resolve",
        {"target_ref": "bank"},
        {"target_ref": "bank", "addresses": ["2001:db8::1", "192.0.2.10"]},
    )

    assert len(mutation.nodes) == 1
    assert mutation.nodes[0].node_type is GraphNodeType.HOST
    assert str(mutation.nodes[0].node_id) == "host:bank.local"
    assert mutation.nodes[0].facts["addresses"] == ["192.0.2.10", "2001:db8::1"]
    assert mutation.edges == []


def test_tls_inspection_creates_a_verified_tls_service() -> None:
    mutation = _map(
        "recon.tls_inspect",
        {"target_ref": "bank"},
        {
            "target_ref": "bank",
            "verified": True,
            "protocol": "TLSv1.3",
            "cipher": "TLS_AES_256_GCM_SHA384",
            "certificate_sha256": "a" * 64,
        },
    )

    assert [node.node_type for node in mutation.nodes] == [GraphNodeType.SERVICE]
    assert str(mutation.nodes[0].node_id) == "service:bank.local/tls"
    assert mutation.nodes[0].facts["certificate_sha256"] == f"sha256:{'a' * 64}"
    assert [edge.edge_type for edge in mutation.edges] == [GraphEdgeType.HOSTS]


def test_oidc_inventory_records_capabilities_without_following_advertised_urls() -> None:
    mutation = _map(
        "recon.oidc_inventory",
        {"target_ref": "bank", "path": "/.well-known/openid-configuration"},
        {
            "target_ref": "bank",
            "capabilities": {
                "response_types_supported": ["code"],
                "grant_types_supported": ["authorization_code"],
                "code_challenge_methods_supported": ["S256"],
                "token_endpoint_auth_methods_supported": ["client_secret_basic"],
            },
            "endpoints_present": {
                "authorization_endpoint": True,
                "token_endpoint": True,
                "jwks_uri": True,
                "revocation_endpoint": False,
            },
            "source_untrusted": True,
            "endpoints_followed": False,
            "verdict": "observation_only",
        },
    )

    assert len(mutation.nodes) == 1
    node = mutation.nodes[0]
    assert node.node_type is GraphNodeType.ENDPOINT
    assert node.observations[0]["endpoints_followed"] is False
    assert [edge.edge_type for edge in mutation.edges] == [GraphEdgeType.OBSERVED]


def test_port_scan_materializes_only_open_services_and_keeps_all_states_as_observation() -> None:
    mutation = _map(
        "recon.port_scan",
        {"target_ref": "bank"},
        {
            "target_ref": "bank",
            "ports": [
                {"port": 443, "protocol": "tcp", "state": "open"},
                {"port": 22, "protocol": "tcp", "state": "closed"},
            ],
            "engine_exit_success": True,
            "verdict": "observation_only",
        },
    )

    assert {node.node_type for node in mutation.nodes} == {
        GraphNodeType.HOST,
        GraphNodeType.SERVICE,
    }
    service = next(node for node in mutation.nodes if node.node_type is GraphNodeType.SERVICE)
    assert str(service.node_id) == "service:bank.local/tcp:443"
    assert len(mutation.edges) == 1
    host = next(node for node in mutation.nodes if node.node_type is GraphNodeType.HOST)
    assert [item["state"] for item in host.observations[0]["ports"]] == ["closed", "open"]


@pytest.mark.parametrize(
    ("tool_id", "arguments", "output"),
    [
        (
            "recon.dns_resolve",
            {"target_ref": "bank"},
            {"target_ref": "other", "addresses": ["192.0.2.10"]},
        ),
        (
            "recon.tls_inspect",
            {"target_ref": "bank"},
            {
                "target_ref": "bank",
                "verified": False,
                "protocol": "TLSv1.3",
                "cipher": "TLS_AES_256_GCM_SHA384",
                "certificate_sha256": "a" * 64,
            },
        ),
        (
            "recon.oidc_inventory",
            {"target_ref": "bank", "path": "/oidc"},
            {
                "target_ref": "bank",
                "capabilities": {},
                "endpoints_present": {},
                "source_untrusted": False,
                "endpoints_followed": False,
                "verdict": "observation_only",
            },
        ),
        (
            "recon.port_scan",
            {"target_ref": "bank"},
            {
                "target_ref": "bank",
                "ports": [{"port": True, "protocol": "tcp", "state": "open"}],
                "engine_exit_success": True,
                "verdict": "observation_only",
            },
        ),
    ],
)
def test_malformed_recon_results_do_not_create_partial_graph_claims(
    tool_id: str, arguments: dict, output: dict
) -> None:
    assert _map(tool_id, arguments, output) == GraphMutation()
