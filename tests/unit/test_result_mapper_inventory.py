"""Result mappers for bounded inventory and session-aware HTTP tools."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from redteam.attack_graph.identity import (
    session_credential_node_id,
    session_principal_node_id,
)
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
SHA256 = "a" * 64


def _result(tool_id: str, output: dict) -> ToolResult:
    return ToolResult(
        invocation_id=InvocationId("inv_inventory"),
        action_id=ActionId("action_inventory"),
        tool_id=ToolId(tool_id),
        success=True,
        output=output,
        started_at=NOW,
        finished_at=NOW,
    )


def _map(tool_id: str, arguments: dict, output: dict) -> GraphMutation:
    return default_result_mapper_registry().map_result(
        tool_id=ToolId(tool_id),
        target=TARGET,
        arguments=arguments,
        result=_result(tool_id, output),
    )


def _html_output() -> dict:
    return {
        "target_ref": "bank",
        "references": [
            {
                "kind": "a",
                "path": "/accounts/123",
                "method": "GET",
                "query_omitted": True,
                "fragment_omitted": False,
                "path_allowlisted": True,
            },
            {
                "kind": "form",
                "path": "/login",
                "method": "POST",
                "query_omitted": False,
                "fragment_omitted": False,
                "path_allowlisted": False,
            },
        ],
        "reference_limit": 200,
        "references_truncated": False,
        "omitted_references": 0,
        "base_element_present": False,
        "decoding_replacements": False,
        "references_followed": False,
        "javascript_executed": False,
        "source_untrusted": True,
        "verdict": "observation_only",
        "source_sha256": SHA256,
        "request_count": 1,
    }


def _openapi_output() -> dict:
    return {
        "target_ref": "bank",
        "operations": [
            {
                "path": "/accounts/{id}",
                "method": "GET",
                "declares_security": True,
                "deprecated": False,
            },
            {
                "path": "/accounts/{id}",
                "method": "POST",
                "declares_security": True,
                "deprecated": False,
            },
        ],
        "limit": 200,
        "may_be_truncated": False,
        "external_refs_followed": False,
        "source_untrusted": True,
        "source_sha256": SHA256,
    }


def _security_output() -> dict:
    return {
        "target_ref": "bank",
        "status_code": 200,
        "headers_present": {
            "strict-transport-security": True,
            "content-security-policy": False,
            "content-security-policy-report-only": False,
            "x-frame-options": True,
            "x-content-type-options": True,
            "referrer-policy": False,
            "permissions-policy": False,
        },
        "cache_control_no_store": True,
        "content_type_nosniff": True,
        "cookies": [
            {
                "parse_status": "parsed",
                "secure": True,
                "http_only": True,
                "same_site": "lax",
                "domain_attribute_present": False,
                "persistent": False,
            }
        ],
        "set_cookie_header_count": 1,
        "cookies_truncated": False,
        "raw_headers_omitted": True,
        "cookie_names_and_values_omitted": True,
        "verdict": "observation_only",
        "request_count": 1,
    }


def test_html_inventory_creates_deduplicated_endpoint_candidates() -> None:
    mutation = _map(
        "recon.html_inventory",
        {"target_ref": "bank", "path": "/"},
        _html_output(),
    )

    assert {str(node.node_id) for node in mutation.nodes} == {
        "endpoint:bank.local GET /accounts/{id}",
        "endpoint:bank.local POST /login",
    }
    account = next(node for node in mutation.nodes if "accounts" in str(node.node_id))
    assert account.facts["observed_target"] == "/accounts/123"
    assert account.facts["path_parameters"] == ["accounts_id"]
    assert account.attrs["object_scoped"] is True
    assert {edge.edge_type for edge in mutation.edges} == {GraphEdgeType.OBSERVED}


def test_openapi_inventory_models_operations_and_one_shared_resource() -> None:
    mutation = _map(
        "recon.openapi_inventory",
        {"target_ref": "bank", "path": "/openapi.json"},
        _openapi_output(),
    )

    endpoints = [node for node in mutation.nodes if node.node_type is GraphNodeType.ENDPOINT]
    resources = [node for node in mutation.nodes if node.node_type is GraphNodeType.RESOURCE]
    assert len(endpoints) == 2
    assert len(resources) == 1
    assert str(resources[0].node_id) == "resource:bank.local /accounts/{id}"
    assert all(node.attrs["scope_unverified"] is True for node in endpoints)
    assert [edge.edge_type for edge in mutation.edges].count(GraphEdgeType.RELATED_TO) == 2
    assert [edge.edge_type for edge in mutation.edges].count(GraphEdgeType.OBSERVED) == 2


def test_session_comparison_models_scoped_principals_and_observed_access() -> None:
    output = {
        "target_ref": "bank",
        "first": {
            "principal_ref": "alice",
            "status_code": 403,
            "body_sha256": "1" * 64,
        },
        "second": {
            "principal_ref": "bob",
            "status_code": 200,
            "body_sha256": "2" * 64,
        },
        "same_status": False,
        "same_body": False,
        "verdict": "observation_only",
        "request_count": 2,
    }
    mutation = _map(
        "web.http_compare_sessions",
        {
            "target_ref": "bank",
            "path": "/accounts/123",
            "first_session_ref": "alice-session",
            "second_session_ref": "bob-session",
        },
        output,
    )

    assert len(mutation.nodes) == 5
    assert session_principal_node_id("bank", "alice-session") in {
        node.node_id for node in mutation.nodes
    }
    assert session_credential_node_id("bank", "bob-session") in {
        node.node_id for node in mutation.nodes
    }
    access = [edge for edge in mutation.edges if edge.edge_type is GraphEdgeType.CAN_ACCESS]
    assert len(access) == 1
    assert access[0].source_id == session_principal_node_id("bank", "bob-session")
    assert access[0].properties["basis"] == "observed_http_success"


def test_security_posture_stores_fixed_flags_without_cookie_identity() -> None:
    mutation = _map(
        "web.http_security_posture",
        {"target_ref": "bank", "path": "/", "session_ref": "alice"},
        _security_output(),
    )

    endpoint = next(node for node in mutation.nodes if node.node_type is GraphNodeType.ENDPOINT)
    observation = endpoint.observations[0]
    assert observation["verdict"] == "observation_only"
    assert observation["cookies"] == [
        {
            "parse_status": "parsed",
            "secure": True,
            "http_only": True,
            "same_site": "lax",
            "domain_attribute_present": False,
            "persistent": False,
        }
    ]
    assert set(observation["cookies"][0]) == {
        "parse_status",
        "secure",
        "http_only",
        "same_site",
        "domain_attribute_present",
        "persistent",
    }
    assert {edge.edge_type for edge in mutation.edges} == {
        GraphEdgeType.RELATED_TO,
        GraphEdgeType.CAN_ACCESS,
    }


@pytest.mark.parametrize(
    ("tool_id", "arguments", "output"),
    [
        (
            "recon.html_inventory",
            {"target_ref": "bank", "path": "/"},
            {**_html_output(), "source_untrusted": False},
        ),
        (
            "recon.openapi_inventory",
            {"target_ref": "bank", "path": "/openapi.json"},
            {**_openapi_output(), "external_refs_followed": True},
        ),
        (
            "web.http_compare_sessions",
            {
                "target_ref": "bank",
                "path": "/accounts/123",
                "first_session_ref": "one",
                "second_session_ref": "two",
            },
            {
                "target_ref": "bank",
                "first": {
                    "principal_ref": "alice",
                    "status_code": 403,
                    "body_sha256": "1" * 64,
                },
                "second": {
                    "principal_ref": "bob",
                    "status_code": 200,
                    "body_sha256": "2" * 64,
                },
                "same_status": "false",
                "same_body": False,
                "verdict": "observation_only",
                "request_count": 2,
            },
        ),
        (
            "web.http_security_posture",
            {"target_ref": "bank", "path": "/"},
            {
                **_security_output(),
                "cookies": [
                    {
                        **_security_output()["cookies"][0],
                        "name": "must-not-enter-graph",
                    }
                ],
            },
        ),
    ],
)
def test_contract_mismatch_is_rejected_without_partial_graph_facts(
    tool_id: str, arguments: dict, output: dict
) -> None:
    assert _map(tool_id, arguments, output) == GraphMutation()


def test_same_session_name_on_different_targets_has_different_ids() -> None:
    assert session_principal_node_id("bank-a", "alice") != session_principal_node_id(
        "bank-b", "alice"
    )
    assert session_credential_node_id("bank-a", "alice") != session_credential_node_id(
        "bank-b", "alice"
    )
