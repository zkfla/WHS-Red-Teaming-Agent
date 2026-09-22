"""``recon.http_probe`` -> ``SERVICE``/``ENDPOINT`` discovery.

Real output shape (verified directly against
``redteam.capability.servers.network.CapturedResponse.summary()``, the
only place this tool's ``output`` dict is built)::

    {
        "target_ref": str,
        "status_code": int,
        "body_bytes": int,
        "body_sha256": str,
        "redirect": bool,
        "security_headers_present": dict[str, bool],
        "body_omitted": True,
    }

Current responses also carry host/port/scheme. Preserve observed ports in
node identity and HTTPS in facts; legacy responses may omit those fields.
The path comes from the actual request, and GET is fixed by this tool.
Missing port/scheme values are never filled from conventional defaults.
"""

from __future__ import annotations

from typing import Any

from redteam.attack_graph.identity import (
    OBSERVED_TARGET_FACT,
    endpoint_node_id,
    node_id_for_target,
    service_node_id,
)
from redteam.core.contracts.target import AnyTarget, HostTarget
from redteam.core.contracts.tool import ToolResult
from redteam.core.enums import GraphEdgeType, GraphNodeType
from redteam.orchestration.observations.mapper import EdgeMutation, GraphMutation, NodeMutation

#: What this tool always speaks -- not inferred from the response.
_SCHEME = "http"
_METHOD = "GET"


def map_recon_http_probe(
    *, target: AnyTarget, arguments: dict[str, Any], result: ToolResult
) -> GraphMutation:
    if not result.success or not isinstance(result.output, dict):
        return GraphMutation()

    if not isinstance(target, HostTarget):
        # This mapper only knows how to place a discovery under a HOST
        # node -- a non-host target (e.g. already an EndpointTarget,
        # which also happens to carry a `.host` attribute) is left
        # alone rather than guessed at.
        return GraphMutation()
    host = target.host
    if not host:
        return GraphMutation()
    if "host" in result.output and result.output["host"] != host:
        return GraphMutation()
    port = result.output.get("port")
    if port is not None and (type(port) is not int or not 1 <= port <= 65535):
        return GraphMutation()
    scheme = result.output.get("scheme")
    if scheme is not None and scheme not in ("http", "https"):
        return GraphMutation()

    status_code = result.output.get("status_code")
    if type(status_code) is not int or not 100 <= status_code <= 599:
        # Incomplete/malformed output: no confirmed response, so no
        # service/endpoint claim -- only the pre-existing HOST attrs
        # (reachable/probed, from PlanStep.observed_attrs) still apply.
        return GraphMutation()

    path = arguments.get("path")
    if not isinstance(path, str) or not path:
        path = "/"

    security_headers = result.output.get("security_headers_present")
    endpoint_facts: dict[str, Any] = {
        "host": host,
        "path": path,
        "method": _METHOD,
        OBSERVED_TARGET_FACT: path,
    }
    service_facts: dict[str, Any] = {"host": host}
    for name, value in (("port", port), ("scheme", scheme)):
        if value is not None:
            endpoint_facts[name] = value
            service_facts[name] = value
    if isinstance(security_headers, dict):
        endpoint_facts["security_headers_present"] = dict(security_headers)

    host_id = node_id_for_target(target)
    service_name = scheme or _SCHEME
    if port is not None:
        service_name = f"{service_name}:{port}"
    service_id = service_node_id(service_name, host=host)
    endpoint_id = endpoint_node_id(_METHOD, path, host=host, port=port)

    service_node = NodeMutation(
        node_type=GraphNodeType.SERVICE,
        node_id=service_id,
        attrs={"responded": True},
        facts={**service_facts, "display_name": f"{host} {service_name}"},
        labels=["discovered-by:recon.http_probe"],
    )
    endpoint_node = NodeMutation(
        node_type=GraphNodeType.ENDPOINT,
        node_id=endpoint_id,
        attrs={
            "status_code": status_code,
            "redirect": bool(result.output.get("redirect", False)),
        },
        facts=endpoint_facts,
        labels=["discovered-by:recon.http_probe"],
    )

    return GraphMutation(
        nodes=[service_node, endpoint_node],
        edges=[
            EdgeMutation(GraphEdgeType.HOSTS, host_id, service_id),
            EdgeMutation(GraphEdgeType.EXPOSES, service_id, endpoint_id),
        ],
    )
