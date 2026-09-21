"""Normalize HTTP response observations without asserting vulnerabilities."""

from __future__ import annotations

from typing import Any

from redteam.attack_graph.identity import endpoint_node_id, path_parameters
from redteam.core.contracts.target import AnyTarget
from redteam.core.contracts.tool import ToolResult
from redteam.core.enums import GraphNodeType
from redteam.orchestration.authentication import http_observation, request_key
from redteam.orchestration.observations.mapper import (
    GraphMutation,
    NodeMutation,
    is_http_status,
)


def map_web_compare_anonymous(
    *, target: AnyTarget, arguments: dict[str, Any], result: ToolResult
) -> GraphMutation:
    """Record what a real anonymous/session comparison measured.

    Only the fields http_compare_anonymous actually returns are mapped: the
    two status codes, whether they matched, and whether the bodies matched.
    The tool declares observation_only and that is what is stored -- a
    permission difference between an anonymous request and a registered test
    principal is evidence, not a confirmed bypass.
    """

    if not result.success or not isinstance(result.output, dict):
        return GraphMutation()
    output = result.output
    host = getattr(target, "host", None)
    path = arguments.get("path")
    session_ref = arguments.get("session_ref")
    anonymous = output.get("anonymous")
    authenticated = output.get("authenticated")
    if (
        not host
        or not isinstance(path, str)
        or not path.startswith("/")
        or not isinstance(session_ref, str)
        or not session_ref
        or not isinstance(anonymous, dict)
        or not isinstance(authenticated, dict)
    ):
        return GraphMutation()
    anonymous_status = anonymous.get("status_code")
    authenticated_status = authenticated.get("status_code")
    if not is_http_status(anonymous_status) or not is_http_status(authenticated_status):
        return GraphMutation()

    port = getattr(target, "port", None)
    comparison = {
        "kind": "http_access_comparison",
        "context_key": request_key("GET", path, session_ref),
        "method": "GET",
        "path": path,
        "target_ref": arguments.get("target_ref"),
        "session_ref": session_ref,
        "anonymous_status": anonymous_status,
        "authenticated_status": authenticated_status,
        "same_status": output.get("same_status") is True,
        "same_body": output.get("same_body") is True,
        "verdict": "observation_only",
    }
    return GraphMutation(
        nodes=[
            NodeMutation(
                node_type=GraphNodeType.ENDPOINT,
                node_id=endpoint_node_id("GET", path, host=host, port=port),
                attrs={"web_inspected": True},
                facts={
                    "host": host,
                    "path": path,
                    "assessment_kind": "observation",
                    **({"port": port} if port is not None else {}),
                },
                labels=["observed-by:web.http_compare_anonymous"],
                observations=[comparison],
            )
        ]
    )


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
        or not is_http_status(status)
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
