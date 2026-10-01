"""Map bounded proxy, ZAP and passive Nuclei observations into the graph."""

from __future__ import annotations

import re
from typing import Any

from redteam.attack_graph.identity import (
    endpoint_node_id,
    flow_resource_node_id,
    node_id_for_target,
    path_parameters,
)
from redteam.core.contracts.target import AnyTarget
from redteam.core.contracts.tool import ToolResult
from redteam.core.enums import GraphEdgeType, GraphNodeType
from redteam.orchestration.observations._shared import deterministic_key, is_hex64, safe_path
from redteam.orchestration.observations.mapper import (
    EdgeMutation,
    GraphMutation,
    NodeMutation,
    is_http_status,
)
from redteam.orchestration.observations.web_http import _session_identity_mutations

_FLOW_REF = re.compile(r"^flow_[0-9a-f]{16}$")
_REFERENCE = re.compile(r"^[A-Za-z0-9_.:@/-]{1,128}$")
_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE"})
_SUMMARY_HEADERS = frozenset(
    {
        "strict-transport-security",
        "content-security-policy",
        "x-content-type-options",
        "cache-control",
    }
)
_NUCLEI_CHECKS = frozenset({"missing-csp", "missing-nosniff-header"})


def _safe_reference(value: Any) -> str | None:
    if not isinstance(value, str) or _REFERENCE.fullmatch(value) is None:
        return None
    if value.isdigit() and len(value) >= 9:
        return None
    return value


def _response_summary(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    status = value.get("status_code")
    body_bytes = value.get("body_bytes")
    digest = value.get("body_sha256")
    redirect = value.get("redirect")
    headers = value.get("security_headers_present")
    if (
        not is_http_status(status)
        or type(body_bytes) is not int
        or body_bytes < 0
        or not is_hex64(digest)
        or type(redirect) is not bool
        or redirect != (300 <= status < 400)
        or not isinstance(headers, dict)
        or set(headers) - _SUMMARY_HEADERS
        or any(type(item) is not bool for item in headers.values())
        or value.get("body_omitted") is not True
    ):
        return None
    return {
        "status_code": status,
        "body_bytes": body_bytes,
        "body_sha256": f"sha256:{digest.lower()}",
        "redirect": redirect,
        "security_headers_present": dict(sorted(headers.items())),
        "body_omitted": True,
    }


def _flow_summary(value: Any, *, expected_target_ref: str | None) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    flow_ref = value.get("flow_ref")
    target_ref = _safe_reference(value.get("target_ref"))
    method = value.get("method")
    path = safe_path(value.get("path"))
    fields = value.get("request_json_fields")
    response = _response_summary(value.get("captured_response"))
    if (
        not isinstance(flow_ref, str)
        or _FLOW_REF.fullmatch(flow_ref) is None
        or target_ref is None
        or (expected_target_ref is not None and target_ref != expected_target_ref)
        or method not in _METHODS
        or path is None
        or not isinstance(fields, list)
        or len(fields) > 100
        or any(
            not isinstance(field, str)
            or not field
            or len(field) > 100
            or any(ord(char) < 32 for char in field)
            for field in fields
        )
        or fields != sorted(set(fields))
        or type(value.get("request_body_omitted")) is not bool
        or response is None
    ):
        return None
    return {
        "flow_ref": flow_ref,
        "target_ref": target_ref,
        "method": method,
        "path": path,
        "request_fields": fields,
        "request_body_omitted": value["request_body_omitted"],
        "captured_response": response,
    }


def _flow_mutation(
    *,
    target: AnyTarget,
    flow: dict[str, Any],
    source_tool: str,
    source_untrusted: bool,
) -> tuple[list[NodeMutation], list[EdgeMutation]]:
    host = getattr(target, "host", None)
    if not isinstance(host, str) or not host:
        return [], []
    flow_ref = flow["flow_ref"]
    target_ref = flow["target_ref"]
    method = flow["method"]
    path = flow["path"]
    response = flow["captured_response"]
    port = getattr(target, "port", None)
    endpoint_id = endpoint_node_id(
        method, path, host=host, port=port
    )
    resource_id = flow_resource_node_id(target_ref, flow_ref)
    params = path_parameters(path)
    observation = {
        "kind": "captured_http_flow",
        "context_key": deterministic_key(source_tool, target_ref, flow_ref),
        "source_tool": source_tool,
        "flow_ref": flow_ref,
        "target_ref": target_ref,
        "method": method,
        "path": path,
        "request_fields": flow["request_fields"],
        "request_body_omitted": flow["request_body_omitted"],
        "captured_response": response,
        "source_untrusted": source_untrusted,
        "verdict": "observation_only",
    }
    endpoint_facts: dict[str, Any] = {
        "host": host,
        "target_ref": target_ref,
        "method": method,
        "path": path,
        "observed_target": path,
        "assessment_kind": "observation",
        "source_untrusted": source_untrusted,
    }
    if port is not None:
        endpoint_facts["port"] = port
    if params:
        endpoint_facts["path_parameters"] = params
    nodes = [
        NodeMutation(
            GraphNodeType.RESOURCE,
            resource_id,
            attrs={"captured_flow": True},
            facts={
                "flow_ref": flow_ref,
                "target_ref": target_ref,
                "method": method,
                "path": path,
                "request_fields": flow["request_fields"],
                "request_body_omitted": flow["request_body_omitted"],
                "source_untrusted": source_untrusted,
            },
            labels=[f"observed-by:{source_tool}"],
        ),
        NodeMutation(
            GraphNodeType.ENDPOINT,
            endpoint_id,
            attrs={
                "proxy_observed": True,
                **({"object_scoped": True} if params else {}),
            },
            facts=endpoint_facts,
            labels=[f"observed-by:{source_tool}"],
            observations=[observation],
        ),
    ]
    target_id = node_id_for_target(target)
    edges = [
        EdgeMutation(GraphEdgeType.OBSERVED, target_id, resource_id),
        EdgeMutation(GraphEdgeType.DERIVED_FROM, endpoint_id, resource_id),
    ]
    return nodes, edges


def _map_flow_batch(
    *,
    target: AnyTarget,
    values: Any,
    expected_target_ref: str | None,
    source_tool: str,
    source_untrusted: bool,
    maximum: int,
) -> GraphMutation:
    if not isinstance(values, list) or len(values) > maximum:
        return GraphMutation()
    nodes: dict[str, NodeMutation] = {}
    edges: dict[tuple[str, str, str], EdgeMutation] = {}
    for value in values:
        flow = _flow_summary(value, expected_target_ref=expected_target_ref)
        if flow is None:
            return GraphMutation()
        flow_nodes, flow_edges = _flow_mutation(
            target=target,
            flow=flow,
            source_tool=source_tool,
            source_untrusted=source_untrusted,
        )
        if not flow_nodes:
            return GraphMutation()
        for node in flow_nodes:
            existing = nodes.get(str(node.node_id))
            if existing is None:
                nodes[str(node.node_id)] = node
            elif node.observations:
                for observation in node.observations:
                    if observation not in existing.observations:
                        existing.observations.append(observation)
        for edge in flow_edges:
            edges[(edge.edge_type.value, str(edge.source_id), str(edge.target_id))] = edge
    return GraphMutation(nodes=list(nodes.values()), edges=list(edges.values()))


def map_web_proxy_list_flows(
    *, target: AnyTarget, arguments: dict[str, Any], result: ToolResult
) -> GraphMutation:
    if not result.success or not isinstance(result.output, dict):
        return GraphMutation()
    target_ref = _safe_reference(arguments.get("target_ref"))
    limit = arguments.get("limit", 50)
    if (
        target_ref is None
        or type(limit) is not int
        or not 1 <= limit <= 100
        or type(result.output.get("may_be_truncated")) is not bool
    ):
        return GraphMutation()
    return _map_flow_batch(
        target=target,
        values=result.output.get("flows"),
        expected_target_ref=target_ref,
        source_tool="web.proxy_list_flows",
        source_untrusted=True,
        maximum=limit,
    )


def map_web_proxy_get_flow(
    *, target: AnyTarget, arguments: dict[str, Any], result: ToolResult
) -> GraphMutation:
    if not result.success or not isinstance(result.output, dict):
        return GraphMutation()
    flow_ref = arguments.get("flow_ref")
    if not isinstance(flow_ref, str) or result.output.get("flow_ref") != flow_ref:
        return GraphMutation()
    flow = _flow_summary(result.output, expected_target_ref=None)
    if flow is None:
        return GraphMutation()
    target_ref = flow["target_ref"]
    resource_id = flow_resource_node_id(target_ref, flow_ref)
    observation = {
        "kind": "captured_http_flow_detail",
        "context_key": deterministic_key("web.proxy_get_flow", target_ref, flow_ref),
        "source_tool": "web.proxy_get_flow",
        **flow,
        "source_untrusted": True,
        "verdict": "observation_only",
    }
    return GraphMutation(
        nodes=[
            NodeMutation(
                GraphNodeType.RESOURCE,
                resource_id,
                attrs={"captured_flow": True},
                facts={
                    "flow_ref": flow_ref,
                    "target_ref": target_ref,
                    "method": flow["method"],
                    "path": flow["path"],
                    "request_fields": flow["request_fields"],
                    "request_body_omitted": flow["request_body_omitted"],
                    "source_untrusted": True,
                },
                labels=["observed-by:web.proxy_get_flow"],
                observations=[observation],
            )
        ],
        edges=[
            EdgeMutation(
                GraphEdgeType.OBSERVED,
                node_id_for_target(target),
                resource_id,
            )
        ],
    )


def map_web_zap_capture_snapshot(
    *, target: AnyTarget, arguments: dict[str, Any], result: ToolResult
) -> GraphMutation:
    if not result.success or not isinstance(result.output, dict):
        return GraphMutation()
    target_ref = _safe_reference(arguments.get("target_ref"))
    output = result.output
    page_limit = output.get("page_limit")
    if (
        target_ref is None
        or output.get("target_ref") != target_ref
        or output.get("provider") != "zap-live-har"
        or type(page_limit) is not int
        or not 1 <= page_limit <= 5000
        or output.get("coverage") != "first_page_only"
        or output.get("target_request_count") != 0
        or output.get("zap_api_request_count") != 1
        or output.get("source_untrusted") is not True
    ):
        return GraphMutation()
    return _map_flow_batch(
        target=target,
        values=output.get("flows"),
        expected_target_ref=target_ref,
        source_tool="web.zap_capture_snapshot",
        source_untrusted=True,
        maximum=page_limit,
    )


def _flow_execution_mutation(
    *,
    target: AnyTarget,
    arguments: dict[str, Any],
    output: dict[str, Any],
    source_tool: str,
    mutated: bool,
) -> GraphMutation:
    flow_ref = arguments.get("flow_ref")
    target_ref = _safe_reference(output.get("target_ref"))
    response = _response_summary(output)
    if (
        not isinstance(flow_ref, str)
        or _FLOW_REF.fullmatch(flow_ref) is None
        or output.get("flow_ref") != flow_ref
        or target_ref is None
        or response is None
    ):
        return GraphMutation()
    mutated_fields: list[str] = []
    if mutated:
        mutations = arguments.get("mutations")
        reported = output.get("mutated_fields")
        if (
            not isinstance(mutations, dict)
            or not mutations
            or len(mutations) > 100
            or any(
                not isinstance(field, str)
                or not field
                or len(field) > 100
                or any(ord(char) < 32 for char in field)
                for field in mutations
            )
            or not isinstance(reported, list)
            or reported != sorted(mutations)
        ):
            return GraphMutation()
        mutated_fields = reported
    resource_id = flow_resource_node_id(target_ref, flow_ref)
    session_ref = arguments.get("session_ref")
    if session_ref is not None:
        session_ref = _safe_reference(session_ref)
        if session_ref is None:
            return GraphMutation()
    observation = {
        "kind": "proxy_flow_execution",
        "context_key": deterministic_key(
            source_tool, target_ref, flow_ref, session_ref, mutated_fields
        ),
        "source_tool": source_tool,
        "flow_ref": flow_ref,
        "target_ref": target_ref,
        "session_ref": session_ref,
        "mutated_fields": mutated_fields,
        "response": response,
        "verdict": "observation_only",
    }
    nodes = [
        NodeMutation(
            GraphNodeType.RESOURCE,
            resource_id,
            attrs={"flow_executed": True, **({"flow_mutated": True} if mutated else {})},
            facts={"flow_ref": flow_ref, "target_ref": target_ref},
            labels=[f"observed-by:{source_tool}"],
            observations=[observation],
        )
    ]
    edges = [
        EdgeMutation(GraphEdgeType.OBSERVED, node_id_for_target(target), resource_id)
    ]
    if isinstance(session_ref, str):
        identity_nodes, identity_edges = _session_identity_mutations(
            target_ref=target_ref,
            session_ref=session_ref,
            endpoint_id=resource_id,
            status=response["status_code"],
        )
        nodes.extend(identity_nodes)
        edges.extend(identity_edges)
    return GraphMutation(nodes=nodes, edges=edges)


def map_web_proxy_replay_flow(
    *, target: AnyTarget, arguments: dict[str, Any], result: ToolResult
) -> GraphMutation:
    if not result.success or not isinstance(result.output, dict):
        return GraphMutation()
    return _flow_execution_mutation(
        target=target,
        arguments=arguments,
        output=result.output,
        source_tool="web.proxy_replay_flow",
        mutated=False,
    )


def map_web_proxy_mutate_json(
    *, target: AnyTarget, arguments: dict[str, Any], result: ToolResult
) -> GraphMutation:
    if not result.success or not isinstance(result.output, dict):
        return GraphMutation()
    return _flow_execution_mutation(
        target=target,
        arguments=arguments,
        output=result.output,
        source_tool="web.proxy_mutate_json",
        mutated=True,
    )


def map_web_proxy_compare_sessions(
    *, target: AnyTarget, arguments: dict[str, Any], result: ToolResult
) -> GraphMutation:
    if not result.success or not isinstance(result.output, dict):
        return GraphMutation()
    output = result.output
    flow_ref = arguments.get("flow_ref")
    first_ref = _safe_reference(arguments.get("first_session_ref"))
    second_ref = _safe_reference(arguments.get("second_session_ref"))
    first = output.get("first")
    second = output.get("second")
    if (
        not isinstance(flow_ref, str)
        or _FLOW_REF.fullmatch(flow_ref) is None
        or output.get("flow_ref") != flow_ref
        or first_ref is None
        or second_ref is None
        or first_ref == second_ref
        or not isinstance(first, dict)
        or not isinstance(second, dict)
        or type(output.get("same_status")) is not bool
        or type(output.get("same_body")) is not bool
        or output.get("verdict") != "observation_only"
        or output.get("request_count") != 2
    ):
        return GraphMutation()
    first_principal = _safe_reference(first.get("principal_ref"))
    second_principal = _safe_reference(second.get("principal_ref"))
    first_response = _response_summary(first)
    second_response = _response_summary(second)
    if (
        first_principal is None
        or second_principal is None
        or first_principal == second_principal
        or first_response is None
        or second_response is None
        or output["same_status"]
        != (first_response["status_code"] == second_response["status_code"])
        or output["same_body"]
        != (first_response["body_sha256"] == second_response["body_sha256"])
    ):
        return GraphMutation()
    target_scope = str(node_id_for_target(target))
    resource_id = flow_resource_node_id(target_scope, flow_ref)
    observation = {
        "kind": "proxy_session_comparison",
        "context_key": deterministic_key(
            "proxy-compare", target_scope, flow_ref, first_ref, second_ref
        ),
        "source_tool": "web.proxy_compare_sessions",
        "flow_ref": flow_ref,
        "first_session_ref": first_ref,
        "second_session_ref": second_ref,
        "first_principal_ref": first_principal,
        "second_principal_ref": second_principal,
        "first_response": first_response,
        "second_response": second_response,
        "same_status": output["same_status"],
        "same_body": output["same_body"],
        "verdict": "observation_only",
    }
    return GraphMutation(
        nodes=[
            NodeMutation(
                GraphNodeType.RESOURCE,
                resource_id,
                attrs={"session_comparison_observed": True},
                facts={"flow_ref": flow_ref, "target_scope_node_id": target_scope},
                labels=["observed-by:web.proxy_compare_sessions"],
                observations=[observation],
            )
        ],
        edges=[
            EdgeMutation(GraphEdgeType.OBSERVED, node_id_for_target(target), resource_id)
        ],
    )


def map_web_nuclei_response_check(
    *, target: AnyTarget, arguments: dict[str, Any], result: ToolResult
) -> GraphMutation:
    if not result.success or not isinstance(result.output, dict):
        return GraphMutation()
    output = result.output
    host = getattr(target, "host", None)
    path = safe_path(arguments.get("path"))
    target_ref = _safe_reference(arguments.get("target_ref"))
    check_id = arguments.get("check_id")
    status = output.get("status_code")
    if (
        not isinstance(host, str)
        or not host
        or path is None
        or target_ref is None
        or output.get("target_ref") != target_ref
        or check_id not in _NUCLEI_CHECKS
        or output.get("check_id") != check_id
        or type(output.get("matched")) is not bool
        or not is_http_status(status)
        or output.get("request_count") != 1
        or output.get("engine_mode") != "passive"
        or output.get("raw_output_omitted") is not True
        or output.get("verdict") != "observation_only"
    ):
        return GraphMutation()
    endpoint_id = endpoint_node_id(
        "GET", path, host=host, port=getattr(target, "port", None)
    )
    observation = {
        "kind": "passive_response_check",
        "context_key": deterministic_key("nuclei", target_ref, path, check_id),
        "source_tool": "web.nuclei_response_check",
        "target_ref": target_ref,
        "path": path,
        "check_id": check_id,
        "matched": output["matched"],
        "status_code": status,
        "engine_mode": "passive",
        "raw_output_omitted": True,
        "verdict": "observation_only",
    }
    facts: dict[str, Any] = {
        "host": host,
        "target_ref": target_ref,
        "method": "GET",
        "path": path,
        "observed_target": path,
        "assessment_kind": "observation",
    }
    port = getattr(target, "port", None)
    if port is not None:
        facts["port"] = port
    nodes = [
        NodeMutation(
            GraphNodeType.ENDPOINT,
            endpoint_id,
            attrs={"passive_check_observed": True, "status_code": status},
            facts=facts,
            labels=["observed-by:web.nuclei_response_check"],
            observations=[observation],
        )
    ]
    edges: list[EdgeMutation] = []
    session_ref = arguments.get("session_ref")
    if session_ref is not None:
        session_ref = _safe_reference(session_ref)
        if session_ref is None:
            return GraphMutation()
        identity_nodes, identity_edges = _session_identity_mutations(
            target_ref=target_ref,
            session_ref=session_ref,
            endpoint_id=endpoint_id,
            status=status,
        )
        nodes.extend(identity_nodes)
        edges.extend(identity_edges)
    return GraphMutation(nodes=nodes, edges=edges)
