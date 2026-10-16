"""Result mappings for bounded proxy, ZAP, and passive Nuclei tools."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from redteam.attack_graph.identity import (
    flow_resource_node_id,
    session_credential_node_id,
    session_principal_node_id,
)
from redteam.core.contracts.target import HostTarget, UrlTarget
from redteam.core.contracts.tool import ToolResult
from redteam.core.enums import GraphEdgeType, GraphNodeType
from redteam.core.ids import ActionId, InvocationId, ToolId
from redteam.orchestration.observations import (
    GraphMutation,
    default_result_mapper_registry,
)

NOW = datetime.now(timezone.utc)
TARGET = HostTarget(host="bank.local")
FLOW_REF = "flow_0123456789abcdef"


def _response(status: int = 200, digest: str = "a" * 64) -> dict:
    return {
        "status_code": status,
        "body_bytes": 17,
        "body_sha256": digest,
        "redirect": 300 <= status < 400,
        "security_headers_present": {
            "strict-transport-security": True,
            "content-security-policy": False,
            "x-content-type-options": True,
            "cache-control": False,
        },
        "body_omitted": True,
    }


def _flow(
    *,
    flow_ref: str = FLOW_REF,
    path: str = "/accounts/123",
    status: int = 200,
) -> dict:
    return {
        "flow_ref": flow_ref,
        "target_ref": "bank",
        "method": "GET",
        "path": path,
        "request_json_fields": [],
        "request_body_omitted": False,
        "captured_response": _response(status),
    }


def _map(tool_id: str, arguments: dict, output: dict) -> GraphMutation:
    result = ToolResult(
        invocation_id=InvocationId("inv_proxy"),
        action_id=ActionId("action_proxy"),
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


def test_proxy_list_maps_redacted_flows_to_resources_and_endpoints() -> None:
    mutation = _map(
        "web.proxy_list_flows",
        {"target_ref": "bank", "limit": 10},
        {"flows": [_flow()], "may_be_truncated": False},
    )

    assert {node.node_type for node in mutation.nodes} == {
        GraphNodeType.RESOURCE,
        GraphNodeType.ENDPOINT,
    }
    endpoint = next(
        node for node in mutation.nodes if node.node_type is GraphNodeType.ENDPOINT
    )
    resource = next(
        node for node in mutation.nodes if node.node_type is GraphNodeType.RESOURCE
    )
    assert str(endpoint.node_id) == "endpoint:bank.local GET /accounts/{id}"
    assert endpoint.facts["observed_target"] == "/accounts/123"
    assert endpoint.attrs["object_scoped"] is True
    assert "status_code" not in endpoint.attrs
    assert endpoint.observations[0]["captured_response"]["status_code"] == 200
    assert resource.node_id == flow_resource_node_id("bank", FLOW_REF)
    assert {edge.edge_type for edge in mutation.edges} == {
        GraphEdgeType.OBSERVED,
        GraphEdgeType.DERIVED_FROM,
    }


def test_browser_navigation_maps_execution_without_page_content() -> None:
    url = "http://bank.local/accounts"
    result = ToolResult(
        invocation_id=InvocationId("inv_browser"),
        action_id=ActionId("action_browser"),
        tool_id=ToolId("web.browser_navigate"),
        success=True,
        output="page content that must not be copied",
        started_at=NOW,
        finished_at=NOW,
    )

    mutation = default_result_mapper_registry().map_result(
        tool_id=ToolId("web.browser_navigate"),
        target=UrlTarget(url=url),
        arguments={"url": url},
        result=result,
    )

    assert len(mutation.nodes) == 1
    node = mutation.nodes[0]
    assert node.attrs["browser_navigated"] is True
    assert node.observations[0]["page_content_omitted"] is True
    assert "page content" not in str(node.observations[0])


def test_multiple_captures_merge_the_endpoint_but_preserve_each_observation() -> None:
    second_ref = "flow_fedcba9876543210"
    mutation = _map(
        "web.proxy_list_flows",
        {"target_ref": "bank", "limit": 10},
        {
            "flows": [
                _flow(status=401),
                _flow(flow_ref=second_ref, status=200),
            ],
            "may_be_truncated": False,
        },
    )

    endpoints = [
        node for node in mutation.nodes if node.node_type is GraphNodeType.ENDPOINT
    ]
    resources = [
        node for node in mutation.nodes if node.node_type is GraphNodeType.RESOURCE
    ]
    assert len(endpoints) == 1
    assert len(resources) == 2
    assert [
        item["captured_response"]["status_code"]
        for item in endpoints[0].observations
    ] == [401, 200]


def test_zap_snapshot_uses_the_same_proxy_flow_boundary() -> None:
    mutation = _map(
        "web.zap_capture_snapshot",
        {"target_ref": "bank"},
        {
            "target_ref": "bank",
            "provider": "zap-live-har",
            "flows": [_flow()],
            "page_limit": 500,
            "coverage": "first_page_only",
            "target_request_count": 0,
            "zap_api_request_count": 1,
            "source_untrusted": True,
        },
    )

    endpoint = next(
        node for node in mutation.nodes if node.node_type is GraphNodeType.ENDPOINT
    )
    assert endpoint.observations[0]["source_tool"] == "web.zap_capture_snapshot"
    assert endpoint.observations[0]["source_untrusted"] is True


def test_get_flow_does_not_guess_an_endpoint_host_without_target_ref_argument() -> None:
    mutation = _map(
        "web.proxy_get_flow",
        {"flow_ref": FLOW_REF},
        _flow(),
    )

    assert [node.node_type for node in mutation.nodes] == [GraphNodeType.RESOURCE]
    assert mutation.nodes[0].node_id == flow_resource_node_id("bank", FLOW_REF)
    assert mutation.nodes[0].observations[0]["path"] == "/accounts/123"
    assert all(node.node_type is not GraphNodeType.ENDPOINT for node in mutation.nodes)


def test_proxy_replay_links_only_successful_session_access_to_the_flow() -> None:
    mutation = _map(
        "web.proxy_replay_flow",
        {"flow_ref": FLOW_REF, "session_ref": "alice"},
        {
            "flow_ref": FLOW_REF,
            "target_ref": "bank",
            **_response(),
            "actual_session_ref": "alice",
        },
    )

    assert session_principal_node_id("bank", "alice") in {
        node.node_id for node in mutation.nodes
    }
    assert session_credential_node_id("bank", "alice") in {
        node.node_id for node in mutation.nodes
    }
    access = [
        edge for edge in mutation.edges if edge.edge_type is GraphEdgeType.CAN_ACCESS
    ]
    assert len(access) == 1
    assert access[0].target_id == flow_resource_node_id("bank", FLOW_REF)

    denied = _map(
        "web.proxy_replay_flow",
        {"flow_ref": FLOW_REF, "session_ref": "alice"},
        {
            "flow_ref": FLOW_REF,
            "target_ref": "bank",
            **_response(403),
            "actual_session_ref": "alice",
        },
    )
    assert all(edge.edge_type is not GraphEdgeType.CAN_ACCESS for edge in denied.edges)


@pytest.mark.parametrize("receipt", [None, "bob"])
def test_proxy_replay_without_matching_actual_receipt_keeps_observation_only(
    receipt: str | None,
) -> None:
    output = {"flow_ref": FLOW_REF, "target_ref": "bank", **_response()}
    output["actual_session_ref"] = receipt
    mutation = _map(
        "web.proxy_replay_flow",
        {"flow_ref": FLOW_REF, "session_ref": "alice"},
        output,
    )

    assert len(mutation.nodes) == 1
    assert mutation.nodes[0].node_type is GraphNodeType.RESOURCE
    assert mutation.nodes[0].observations[0]["verdict"] == "observation_only"
    assert all(
        node.node_id not in {
            session_principal_node_id("bank", "alice"),
            session_credential_node_id("bank", "alice"),
        }
        for node in mutation.nodes
    )
    assert all(edge.edge_type is not GraphEdgeType.CAN_ACCESS for edge in mutation.edges)


def test_proxy_replay_missing_receipt_does_not_infer_identity_from_requested_session() -> None:
    mutation = _map(
        "web.proxy_replay_flow",
        {"flow_ref": FLOW_REF, "session_ref": "alice"},
        {"flow_ref": FLOW_REF, "target_ref": "bank", **_response()},
    )

    assert len(mutation.nodes) == 1
    assert "actual_session_ref" not in mutation.nodes[0].observations[0]["response"]
    assert all(edge.edge_type is not GraphEdgeType.CAN_ACCESS for edge in mutation.edges)


def test_proxy_mutation_stores_field_names_but_never_argument_values() -> None:
    mutation = _map(
        "web.proxy_mutate_json",
        {
            "flow_ref": FLOW_REF,
            "session_ref": "alice",
            "mutations": {"card_number": "4111111111111111"},
        },
        {
            "flow_ref": FLOW_REF,
            "target_ref": "bank",
            "mutated_fields": ["card_number"],
            **_response(),
            "actual_session_ref": "alice",
        },
    )

    encoded = repr(mutation)
    assert "card_number" in encoded
    assert "4111111111111111" not in encoded
    assert session_principal_node_id("bank", "alice") in {
        node.node_id for node in mutation.nodes
    }
    assert any(edge.edge_type is GraphEdgeType.CAN_ACCESS for edge in mutation.edges)


def test_proxy_session_comparison_remains_an_observation() -> None:
    mutation = _map(
        "web.proxy_compare_sessions",
        {
            "flow_ref": FLOW_REF,
            "first_session_ref": "alice",
            "second_session_ref": "bob",
        },
        {
            "flow_ref": FLOW_REF,
            "first": {
                "principal_ref": "user-a",
                **_response(403, "a" * 64),
                "actual_session_ref": "alice",
            },
            "second": {
                "principal_ref": "user-b",
                **_response(200, "b" * 64),
                "actual_session_ref": "bob",
            },
            "same_status": False,
            "same_body": False,
            "verdict": "observation_only",
            "request_count": 2,
        },
    )

    assert [node.node_type for node in mutation.nodes] == [GraphNodeType.RESOURCE]
    assert mutation.nodes[0].observations[0]["verdict"] == "observation_only"
    assert mutation.nodes[0].observations[0]["first_actual_session_ref"] == "alice"
    assert mutation.nodes[0].observations[0]["second_actual_session_ref"] == "bob"
    assert all(edge.edge_type is not GraphEdgeType.CAN_ACCESS for edge in mutation.edges)


def test_proxy_session_comparison_keeps_mismatched_receipt_without_identity_attribution() -> None:
    mutation = _map(
        "web.proxy_compare_sessions",
        {
            "flow_ref": FLOW_REF,
            "first_session_ref": "alice",
            "second_session_ref": "bob",
        },
        {
            "flow_ref": FLOW_REF,
            "first": {
                "principal_ref": "user-a",
                **_response(403, "a" * 64),
                "actual_session_ref": "wrong-session",
            },
            "second": {
                "principal_ref": "user-b",
                **_response(200, "b" * 64),
                "actual_session_ref": "bob",
            },
            "same_status": False,
            "same_body": False,
            "verdict": "observation_only",
            "request_count": 2,
        },
    )

    observation = mutation.nodes[0].observations[0]
    assert observation["first_response"]["actual_session_ref"] == "wrong-session"
    assert "first_principal_ref" not in observation
    assert observation["second_principal_ref"] == "user-b"
    assert all(edge.edge_type is not GraphEdgeType.CAN_ACCESS for edge in mutation.edges)
    assert all(node.node_type is not GraphNodeType.FINDING for node in mutation.nodes)


def test_passive_nuclei_match_does_not_create_a_finding() -> None:
    mutation = _map(
        "web.nuclei_response_check",
        {
            "target_ref": "bank",
            "path": "/accounts/123",
            "check_id": "missing-csp",
            "session_ref": "alice",
        },
        {
            "target_ref": "bank",
            "check_id": "missing-csp",
            "matched": True,
            "status_code": 200,
            "request_count": 1,
            "engine_mode": "passive",
            "raw_output_omitted": True,
            "verdict": "observation_only",
        },
    )

    endpoint = next(
        node for node in mutation.nodes if node.node_type is GraphNodeType.ENDPOINT
    )
    assert endpoint.observations[0]["matched"] is True
    assert endpoint.observations[0]["verdict"] == "observation_only"
    assert all(node.node_type is not GraphNodeType.FINDING for node in mutation.nodes)
    assert all(node.node_type is not GraphNodeType.PRINCIPAL for node in mutation.nodes)
    assert all(edge.edge_type is not GraphEdgeType.CAN_ACCESS for edge in mutation.edges)


@pytest.mark.parametrize(
    ("session_ref", "actual_session_ref"),
    [("alice", "alice"), (None, None)],
)
def test_passive_nuclei_preserves_matching_actual_session_receipt_without_identity(
    session_ref: str | None, actual_session_ref: str | None
) -> None:
    output = {
        "target_ref": "bank",
        "check_id": "missing-csp",
        "matched": True,
        "status_code": 200,
        "request_count": 1,
        "engine_mode": "passive",
        "raw_output_omitted": True,
        "verdict": "observation_only",
        "actual_session_ref": actual_session_ref,
    }
    mutation = _map(
        "web.nuclei_response_check",
        {
            "target_ref": "bank",
            "path": "/accounts/123",
            "check_id": "missing-csp",
            **({"session_ref": session_ref} if session_ref is not None else {}),
        },
        output,
    )

    observation = mutation.nodes[0].observations[0]
    assert observation["actual_session_ref"] == actual_session_ref
    assert all(node.node_type is not GraphNodeType.PRINCIPAL for node in mutation.nodes)
    assert all(node.node_type is not GraphNodeType.FINDING for node in mutation.nodes)
    assert all(edge.edge_type is not GraphEdgeType.CAN_ACCESS for edge in mutation.edges)


@pytest.mark.parametrize(
    ("session_ref", "actual_session_ref"),
    [("alice", None), ("alice", "bob")],
)
def test_passive_nuclei_keeps_missing_or_mismatched_receipt_unknown(
    session_ref: str, actual_session_ref: str | None
) -> None:
    output = {
        "target_ref": "bank",
        "check_id": "missing-csp",
        "matched": True,
        "status_code": 200,
        "request_count": 1,
        "engine_mode": "passive",
        "raw_output_omitted": True,
        "verdict": "observation_only",
    }
    if actual_session_ref is not None:
        output["actual_session_ref"] = actual_session_ref
    mutation = _map(
        "web.nuclei_response_check",
        {
            "target_ref": "bank",
            "path": "/accounts/123",
            "check_id": "missing-csp",
            "session_ref": session_ref,
        },
        output,
    )

    observation = mutation.nodes[0].observations[0]
    assert observation["kind"] == "passive_response_check"
    assert "actual_session_ref" not in observation
    assert all(node.node_type is not GraphNodeType.PRINCIPAL for node in mutation.nodes)
    assert all(node.node_type is not GraphNodeType.FINDING for node in mutation.nodes)
    assert all(edge.edge_type is not GraphEdgeType.CAN_ACCESS for edge in mutation.edges)


@pytest.mark.parametrize(
    ("tool_id", "arguments", "output"),
    [
        (
            "web.proxy_list_flows",
            {"target_ref": "bank", "limit": 10},
            {"flows": [{**_flow(), "target_ref": "other"}], "may_be_truncated": False},
        ),
        (
            "web.proxy_get_flow",
            {"flow_ref": FLOW_REF},
            {**_flow(), "flow_ref": "flow_fedcba9876543210"},
        ),
        (
            "web.proxy_replay_flow",
            {"flow_ref": FLOW_REF},
            {
                "flow_ref": FLOW_REF,
                "target_ref": "bank",
                **_response(302),
                "redirect": False,
            },
        ),
        (
            "web.zap_capture_snapshot",
            {"target_ref": "bank"},
            {
                "target_ref": "bank",
                "provider": "zap-live-har",
                "flows": [],
                "page_limit": 500,
                "coverage": "all_pages",
                "target_request_count": 0,
                "zap_api_request_count": 1,
                "source_untrusted": True,
            },
        ),
        (
            "web.nuclei_response_check",
            {"target_ref": "bank", "path": "/", "check_id": "arbitrary-template"},
            {
                "target_ref": "bank",
                "check_id": "arbitrary-template",
                "matched": True,
                "status_code": 200,
                "request_count": 1,
                "engine_mode": "active",
                "raw_output_omitted": False,
                "verdict": "finding",
            },
        ),
    ],
)
def test_untrusted_or_inconsistent_outputs_create_no_partial_claims(
    tool_id: str, arguments: dict, output: dict
) -> None:
    assert _map(tool_id, arguments, output) == GraphMutation()
