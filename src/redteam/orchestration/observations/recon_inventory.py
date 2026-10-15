"""Map bounded HTML/OpenAPI inventories into graph surface candidates."""

from __future__ import annotations

from typing import Any

from redteam.attack_graph.identity import (
    api_resource_node_id,
    endpoint_node_id,
    node_id_for_target,
    path_parameters,
)
from redteam.core.contracts.target import AnyTarget
from redteam.core.contracts.tool import ToolResult
from redteam.core.enums import GraphEdgeType, GraphNodeType
from redteam.core.ids import GraphNodeId
from redteam.orchestration.observations._shared import (
    deterministic_key,
    is_hex64,
    safe_path,
)
from redteam.orchestration.observations.mapper import EdgeMutation, GraphMutation, NodeMutation

_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE"})


def _context_key(kind: str, host: str, method: str, path: str, source_path: str) -> str:
    """Same content-hash core as every other mapper's ``deterministic_key``,
    with this mapper's own normalization (case-insensitive host) applied
    before hashing -- kept as a thin, named wrapper rather than inlined at
    every call site below."""
    return deterministic_key(kind, host.lower(), method, path, source_path)


def _common_context(
    *, target: AnyTarget, arguments: dict[str, Any], result: ToolResult
) -> tuple[str, int | None, str, str, dict[str, Any]] | None:
    if not result.success or not isinstance(result.output, dict):
        return None
    host = getattr(target, "host", None)
    source_path = safe_path(arguments.get("path"))
    target_ref = arguments.get("target_ref")
    if (
        not isinstance(host, str)
        or not host
        or source_path is None
        or not isinstance(target_ref, str)
        or not target_ref
        or result.output.get("target_ref") != target_ref
    ):
        return None
    return host, getattr(target, "port", None), source_path, target_ref, result.output


def _source_edge(
    source_id: GraphNodeId, discovered_id: GraphNodeId
) -> list[EdgeMutation]:
    if str(source_id) == str(discovered_id):
        return []
    # Same canonical relation snapshot the HOSTS mappers write, so the shared
    # grounding reader can consume it. It proves only that the source document
    # held a same-origin reference, not reachability or content.
    return [
        EdgeMutation(
            GraphEdgeType.OBSERVED,
            source_id,
            discovered_id,
            {
                "relation": {
                    "type": GraphEdgeType.OBSERVED.value,
                    "source_node_id": str(source_id),
                    "target_node_id": str(discovered_id),
                },
                "discovery_kind": "surface_discovery",
            },
        )
    ]


def map_recon_html_inventory(
    *, target: AnyTarget, arguments: dict[str, Any], result: ToolResult
) -> GraphMutation:
    """Materialize only same-origin references already sanitized by Capability."""

    context = _common_context(target=target, arguments=arguments, result=result)
    if context is None:
        return GraphMutation()
    host, port, source_path, target_ref, output = context
    references = output.get("references")
    if (
        not isinstance(references, list)
        or len(references) > 200
        or output.get("reference_limit") != 200
        or type(output.get("references_truncated")) is not bool
        or type(output.get("base_element_present")) is not bool
        or type(output.get("decoding_replacements")) is not bool
        or output.get("references_followed") is not False
        or output.get("javascript_executed") is not False
        or output.get("source_untrusted") is not True
        or output.get("verdict") != "observation_only"
        or output.get("request_count") != 1
        or not is_hex64(output.get("source_sha256"))
        or type(output.get("omitted_references")) is not int
        or output["omitted_references"] < 0
        or (output["references_truncated"] and len(references) != 200)
    ):
        return GraphMutation()

    source_id = node_id_for_target(target)
    nodes: dict[str, NodeMutation] = {}
    edges: dict[tuple[str, str, str], EdgeMutation] = {}
    for item in references:
        if not isinstance(item, dict):
            continue
        path = safe_path(item.get("path"))
        method = item.get("method")
        kind = item.get("kind")
        if (
            path is None
            or method not in {"GET", "POST"}
            or kind not in {"a", "form", "script", "link"}
            or type(item.get("path_allowlisted")) is not bool
            or type(item.get("query_omitted")) is not bool
            or type(item.get("fragment_omitted")) is not bool
        ):
            continue
        endpoint_id = endpoint_node_id(method, path, host=host, port=port)
        params = path_parameters(path)
        facts: dict[str, Any] = {
            "host": host,
            "path": path,
            "method": method,
            "observed_target": path,
            "target_ref": target_ref,
            "source_document_path": source_path,
            "reference_kinds": [kind],
            "path_allowlisted": item["path_allowlisted"],
            "query_omitted": item["query_omitted"],
            "fragment_omitted": item["fragment_omitted"],
            "source_untrusted": True,
            "source_document_sha256": f"sha256:{output['source_sha256'].lower()}",
        }
        if port is not None:
            facts["port"] = port
        if params:
            facts["path_parameters"] = params
        observation = {
            "kind": "endpoint_discovery",
            "context_key": _context_key("html", host, method, path, source_path),
            "source_tool": "recon.html_inventory",
            "source_path": source_path,
            "method": method,
            "path": path,
            "path_allowlisted": item["path_allowlisted"],
        }
        candidate = NodeMutation(
            node_type=GraphNodeType.ENDPOINT,
            node_id=endpoint_id,
            attrs={
                "discovered": True,
                "path_allowlisted": item["path_allowlisted"],
                **({"object_scoped": True} if params else {}),
            },
            facts=facts,
            labels=["discovered-by:recon.html_inventory"],
            observations=[observation],
        )
        existing = nodes.get(str(endpoint_id))
        if existing is None:
            nodes[str(endpoint_id)] = candidate
        else:
            existing.facts["reference_kinds"] = sorted(
                {
                    *existing.facts.get("reference_kinds", []),
                    kind,
                }
            )
            existing.attrs["path_allowlisted"] = (
                existing.attrs.get("path_allowlisted") is True
                or item["path_allowlisted"]
            )
            if observation not in existing.observations:
                existing.observations.append(observation)
        for edge in _source_edge(source_id, endpoint_id):
            edges[(edge.edge_type.value, str(edge.source_id), str(edge.target_id))] = edge

    return GraphMutation(nodes=list(nodes.values()), edges=list(edges.values()))


def map_recon_openapi_inventory(
    *, target: AnyTarget, arguments: dict[str, Any], result: ToolResult
) -> GraphMutation:
    """Map declared API operations without treating the untrusted document as authorization."""

    context = _common_context(target=target, arguments=arguments, result=result)
    if context is None:
        return GraphMutation()
    host, port, source_path, target_ref, output = context
    operations = output.get("operations")
    if (
        not isinstance(operations, list)
        or len(operations) > 200
        or output.get("limit") != 200
        or type(output.get("may_be_truncated")) is not bool
        or output.get("external_refs_followed") is not False
        or output.get("source_untrusted") is not True
        or (output["may_be_truncated"] and len(operations) != 200)
    ):
        return GraphMutation()
    source_sha256 = output.get("source_sha256")
    if not is_hex64(source_sha256):
        return GraphMutation()

    source_id = node_id_for_target(target)
    nodes: dict[str, NodeMutation] = {}
    edges: dict[tuple[str, str, str], EdgeMutation] = {}
    for item in operations:
        if not isinstance(item, dict):
            continue
        path = safe_path(item.get("path"))
        method = item.get("method")
        if (
            path is None
            or method not in _METHODS
            or type(item.get("declares_security")) is not bool
            or type(item.get("deprecated")) is not bool
        ):
            continue
        endpoint_id = endpoint_node_id(method, path, host=host, port=port)
        resource_id = api_resource_node_id(host, path, port)
        params = path_parameters(path)
        endpoint_facts: dict[str, Any] = {
            "host": host,
            "path": path,
            "method": method,
            "observed_target": path,
            "target_ref": target_ref,
            "source_document_path": source_path,
            "source_document_sha256": f"sha256:{source_sha256.lower()}",
            "source_untrusted": True,
            "external_refs_followed": False,
            "inventory_may_be_truncated": output["may_be_truncated"],
            "declares_security": item["declares_security"],
            "deprecated": item["deprecated"],
        }
        if port is not None:
            endpoint_facts["port"] = port
        if params:
            endpoint_facts["path_parameters"] = params
        observation = {
            "kind": "endpoint_discovery",
            "context_key": _context_key("openapi", host, method, path, source_path),
            "source_tool": "recon.openapi_inventory",
            "source_path": source_path,
            "method": method,
            "path": path,
            "declares_security": item["declares_security"],
            "deprecated": item["deprecated"],
        }
        nodes[str(endpoint_id)] = NodeMutation(
            node_type=GraphNodeType.ENDPOINT,
            node_id=endpoint_id,
            attrs={
                "discovered": True,
                "scope_unverified": True,
                **({"object_scoped": True} if params else {}),
            },
            facts=endpoint_facts,
            labels=["declared-by:recon.openapi_inventory"],
            observations=[observation],
        )
        nodes.setdefault(
            str(resource_id),
            NodeMutation(
                node_type=GraphNodeType.RESOURCE,
                node_id=resource_id,
                attrs={"api_resource_template": True},
                facts={
                    "host": host,
                    "path_template": path,
                    "target_ref": target_ref,
                    "source_document_path": source_path,
                    "path_parameters": params,
                    "source_untrusted": True,
                    "source_document_sha256": f"sha256:{source_sha256.lower()}",
                },
                labels=["declared-by:recon.openapi_inventory"],
            ),
        )
        for edge in _source_edge(source_id, endpoint_id):
            edges[(edge.edge_type.value, str(edge.source_id), str(edge.target_id))] = edge
        relation = EdgeMutation(
            GraphEdgeType.RELATED_TO,
            endpoint_id,
            resource_id,
            {"relation": "operates_on"},
        )
        edges[
            (relation.edge_type.value, str(relation.source_id), str(relation.target_id))
        ] = relation

    return GraphMutation(nodes=list(nodes.values()), edges=list(edges.values()))
