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

That server deliberately never returns the resolved host/port/scheme
back to the model ("No raw response, header values, URL query or token
is returned" -- see ``network.py``): only ``status_code`` and the
security-header booleans are real, observed facts about the target this
mapper can use. ``host``/``path`` are not guessed from that output --
they come from the ``ActionProposal.target`` actually probed and the
``path`` argument actually sent, both already known to the framework
independently of the tool's response. ``scheme="GET"``/``method="GET"``
are not read from anywhere either -- they are what *this specific tool*
always does (a plain HTTP GET, per its own implementation), the same
kind of tool-identity fact ``ResultMapperRegistry`` already keys on.
Port is not encoded anywhere: this tool's output never reveals it, so
claiming one would be exactly the invented-field this mapper must not
do (see the module docstring on ``orchestration.observations``).
"""

from __future__ import annotations

from typing import Any

from redteam.attack_graph.identity import endpoint_node_id, host_node_id, service_node_id
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

    status_code = result.output.get("status_code")
    if not isinstance(status_code, int):
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
        "observed_target": path,
        "method": _METHOD,
        "scheme": _SCHEME,
    }
    if isinstance(security_headers, dict):
        endpoint_facts["security_headers_present"] = dict(security_headers)

    host_id = host_node_id(host)
    service_id = service_node_id(_SCHEME, host=host)
    endpoint_id = endpoint_node_id(_METHOD, path, host=host)

    service_node = NodeMutation(
        node_type=GraphNodeType.SERVICE,
        node_id=service_id,
        attrs={"responded": True},
        facts={"host": host, "scheme": _SCHEME, "display_name": f"{host} {_SCHEME}"},
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
