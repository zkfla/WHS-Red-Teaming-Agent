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
independently of the tool's response. GET and the HTTP application
protocol are fixed by this specific tool. The URL scheme and port are
not returned, so the mapper does not claim either one.
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

    host_id = host_node_id(host)
    service_id = service_node_id(_APPLICATION_PROTOCOL, host=host)
    endpoint_id = endpoint_node_id(_METHOD, path, host=host)

    service_node = NodeMutation(
        node_type=GraphNodeType.SERVICE,
        node_id=service_id,
        attrs={"responded": True},
        facts={
            "host": host,
            "application_protocol": _APPLICATION_PROTOCOL,
            "display_name": f"{host} {_APPLICATION_PROTOCOL}",
            "target_ref": output_target_ref,
        },
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
