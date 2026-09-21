"""Normalize HTTP response observations without asserting vulnerabilities."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit, urlunsplit

from redteam.attack_graph.identity import endpoint_node_id, path_parameters
from redteam.core.contracts.target import AnyTarget
from redteam.core.contracts.tool import ToolResult
from redteam.core.enums import GraphNodeType
from redteam.orchestration.authentication import http_observation
from redteam.orchestration.observations.mapper import GraphMutation, NodeMutation


def map_web_http_request(
    *, target: AnyTarget, arguments: dict[str, Any], result: ToolResult
) -> GraphMutation:
    if not result.success or not isinstance(result.output, dict):
        return GraphMutation()
    host = getattr(target, "host", None)
    path = arguments.get("path")
    method = arguments.get("method", "GET")
    status = result.output.get("status_code")
    if (
        not host
        or not isinstance(path, str)
        or not path.startswith("/")
        or not isinstance(method, str)
        or method not in {"GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE"}
        or type(status) is not int
        or not 100 <= status <= 599
    ):
        return GraphMutation()

    facts: dict[str, Any] = {
        "host": host,
        "path": path,
        "method": method,
        "observed_target": path,
        "assessment_kind": "observation",
    }
    parameters = path_parameters(path)
    if parameters:
        facts["path_parameters"] = parameters
    headers = result.output.get("security_headers_present")
    if getattr(target, "port", None) is not None:
        facts["port"] = getattr(target, "port", None)
    observation = http_observation(arguments, result)
    if isinstance(headers, dict):
        facts["security_headers_present"] = {
            key: value
            for key, value in headers.items()
            if key
            in {
                "strict-transport-security",
                "content-security-policy",
                "x-content-type-options",
                "cache-control",
            }
            and type(value) is bool
        }
    return GraphMutation(
        nodes=[
            NodeMutation(
                node_type=GraphNodeType.ENDPOINT,
                node_id=endpoint_node_id(
                    method, path, host=host, port=getattr(target, "port", None)
                ),
                attrs={
                    "web_inspected": True,
                    "status_code": status,
                    "redirect": 300 <= status < 400,
                    **({"object_scoped": True} if parameters else {}),
                },
                facts=facts,
                labels=["observed-by:web.http_request"],
                observations=[observation] if observation else [],
            )
        ]
    )
