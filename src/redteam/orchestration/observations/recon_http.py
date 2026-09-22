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

Current responses also carry the resolved host, port, and scheme. They
are validated against the executed target before becoming graph identity;
legacy responses may omit port and scheme. The path comes from the actual
request, and GET is fixed by this tool.
"""

from __future__ import annotations

import re
from typing import Any

from redteam.attack_graph.identity import endpoint_node_id, host_node_id, service_node_id
from redteam.core.contracts.target import AnyTarget, HostTarget
from redteam.core.contracts.tool import ToolResult
from redteam.core.enums import GraphEdgeType, GraphNodeType
from redteam.orchestration.authentication import request_key
from redteam.orchestration.observations.mapper import (
    EdgeMutation,
    GraphMutation,
    NodeMutation,
    is_http_status,
)

_APPLICATION_PROTOCOL = "http"
_METHOD = "GET"
_HEX_64 = re.compile(r"^[0-9a-fA-F]{64}$")


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

    output = result.output
    if "host" in output and output["host"] != host:
        return GraphMutation()
    port = output.get("port")
    if port is not None and (type(port) is not int or not 1 <= port <= 65535):
        return GraphMutation()
    scheme = output.get("scheme")
    if scheme is not None and scheme not in {"http", "https"}:
        return GraphMutation()
    status_code = output.get("status_code")
    target_ref = arguments.get("target_ref")
    output_target_ref = output.get("target_ref")
    redirect = output.get("redirect")
    security_headers = output.get("security_headers_present")
    digest = output.get("body_sha256")
    if (
        not is_http_status(status_code)
        or not isinstance(output_target_ref, str)
        or not output_target_ref
        or (target_ref is not None and target_ref != output_target_ref)
        or type(redirect) is not bool
        or redirect != (300 <= status_code < 400)
        or not isinstance(security_headers, dict)
        or any(type(value) is not bool for value in security_headers.values())
        or set(security_headers)
        - {
            "strict-transport-security",
            "content-security-policy",
            "x-content-type-options",
            "cache-control",
        }
        or not isinstance(digest, str)
        or _HEX_64.fullmatch(digest) is None
        or output.get("body_omitted") is not True
        or type(output.get("body_bytes")) is not int
        or output["body_bytes"] < 0
    ):
        # Incomplete/malformed output: no confirmed response, so no
        # service/endpoint claim -- only the pre-existing HOST attrs
        # (reachable/probed, from PlanStep.observed_attrs) still apply.
        return GraphMutation()

    path = arguments.get("path", "/")
    if path is None:
        path = "/"
    if (
        not isinstance(path, str)
        or not path.startswith("/")
        or len(path) > 512
        or any(ord(char) < 32 or char in "?#%\\" for char in path)
        or "//" in path
        or any(part in {".", ".."} for part in path.split("/"))
    ):
        return GraphMutation()

    endpoint_facts: dict[str, Any] = {
        "host": host,
        "path": path,
        "observed_target": path,
        "method": _METHOD,
        "target_ref": output_target_ref,
        "security_headers_present": dict(security_headers),
    }
    service_facts: dict[str, Any] = {
        "host": host,
        "application_protocol": _APPLICATION_PROTOCOL,
        "target_ref": output_target_ref,
    }
    if port is not None:
        endpoint_facts["port"] = port
        service_facts["port"] = port
    if scheme is not None:
        endpoint_facts["scheme"] = scheme
        service_facts["scheme"] = scheme

    host_id = host_node_id(host)
    service_name = scheme or _APPLICATION_PROTOCOL
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
            "redirect": redirect,
        },
        facts=endpoint_facts,
        labels=["discovered-by:recon.http_probe"],
        observations=[
            {
                "kind": "http_probe",
                "context_key": request_key(_METHOD, path, None),
                "method": _METHOD,
                "path": path,
                "target_ref": output_target_ref,
                "status_code": status_code,
                "redirect": redirect,
                "security_headers_present": dict(security_headers),
                "body_sha256": f"sha256:{digest.lower()}",
            }
        ],
    )

    return GraphMutation(
        nodes=[service_node, endpoint_node],
        edges=[
            EdgeMutation(GraphEdgeType.HOSTS, host_id, service_id),
            EdgeMutation(GraphEdgeType.EXPOSES, service_id, endpoint_id),
        ],
    )
