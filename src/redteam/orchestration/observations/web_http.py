"""Normalize HTTP response observations without asserting vulnerabilities."""

from __future__ import annotations

import re
from typing import Any

from redteam.attack_graph.identity import (
    endpoint_node_id,
    node_id_for_target,
    path_parameters,
    session_credential_node_id,
    session_principal_node_id,
)
from redteam.core.contracts.target import AnyTarget, HostTarget
from redteam.core.contracts.tool import ToolResult
from redteam.core.enums import GraphEdgeType, GraphNodeType
from redteam.core.ids import GraphNodeId
from redteam.orchestration.authentication import http_observation, request_key
from redteam.orchestration.observations._shared import deterministic_key, is_hex64
from redteam.orchestration.observations.mapper import (
    EdgeMutation,
    GraphMutation,
    NodeMutation,
    is_http_status,
)
from redteam.orchestration.session_proof import original_session_receipt_matches

_REFERENCE = re.compile(r"^[A-Za-z0-9_.:@/-]{1,128}$")
_SECURITY_HEADERS = frozenset(
    {
        "strict-transport-security",
        "content-security-policy",
        "content-security-policy-report-only",
        "x-frame-options",
        "x-content-type-options",
        "referrer-policy",
        "permissions-policy",
    }
)


def _safe_reference(value: Any) -> str | None:
    if not isinstance(value, str) or not _REFERENCE.fullmatch(value):
        return None
    if value.isdigit() and len(value) >= 9:
        return None
    return value


def _endpoint_context(
    target: AnyTarget, arguments: dict[str, Any], output: dict[str, Any]
) -> tuple[str, int | None, str, str] | None:
    host = getattr(target, "host", None)
    path = arguments.get("path")
    target_ref = _safe_reference(arguments.get("target_ref"))
    if (
        not isinstance(host, str)
        or not host
        or not isinstance(path, str)
        or not path.startswith("/")
        or len(path) > 512
        or any(ord(char) < 32 or char in "?#%\\" for char in path)
        or "//" in path
        or any(part in {".", ".."} for part in path.split("/"))
        or target_ref is None
        or output.get("target_ref") != target_ref
    ):
        return None
    return host, getattr(target, "port", None), path, target_ref


def _comparison_key(kind: str, path: str, *session_refs: str) -> str:
    """Domain-named wrapper over the shared ``deterministic_key`` core."""
    return deterministic_key(kind, path, *session_refs)


def _session_identity_mutations(
    *,
    target_ref: str,
    session_ref: str,
    endpoint_id: GraphNodeId,
    status: int,
    principal_ref: str | None = None,
) -> tuple[list[NodeMutation], list[EdgeMutation]]:
    principal_id = session_principal_node_id(target_ref, session_ref)
    credential_id = session_credential_node_id(target_ref, session_ref)
    principal_facts: dict[str, Any] = {
        "target_ref": target_ref,
        "session_refs": [session_ref],
    }
    if principal_ref is not None:
        principal_facts["principal_ref"] = principal_ref
    nodes = [
        NodeMutation(
            node_type=GraphNodeType.PRINCIPAL,
            node_id=principal_id,
            attrs={"operator_managed_session": True},
            facts=principal_facts,
            labels=["observed-session-principal"],
        ),
        NodeMutation(
            node_type=GraphNodeType.CREDENTIAL,
            node_id=credential_id,
            attrs={"reference_only": True},
            facts={
                "target_ref": target_ref,
                "session_ref": session_ref,
                "credential_kind": "session_reference",
            },
            labels=["operator-managed-session-reference"],
        ),
    ]
    edges = [
        EdgeMutation(
            GraphEdgeType.RELATED_TO,
            credential_id,
            principal_id,
            {"relation": "session_for"},
        )
    ]
    if 200 <= status < 300:
        edges.append(
            EdgeMutation(
                GraphEdgeType.CAN_ACCESS,
                principal_id,
                endpoint_id,
                {"status_code": status, "basis": "observed_http_success"},
            )
        )
    return nodes, edges


def map_web_compare_sessions(
    *, target: AnyTarget, arguments: dict[str, Any], result: ToolResult
) -> GraphMutation:
    """Record a two-principal comparison as identities, access edges and observation."""

    if not result.success or not isinstance(result.output, dict):
        return GraphMutation()
    output = result.output
    context = _endpoint_context(target, arguments, output)
    first_ref = _safe_reference(arguments.get("first_session_ref"))
    second_ref = _safe_reference(arguments.get("second_session_ref"))
    first = output.get("first")
    second = output.get("second")
    if (
        context is None
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
    first_status = first.get("status_code")
    second_status = second.get("status_code")
    raw = result.model_dump(mode="json")
    first_receipt_matches = original_session_receipt_matches(
        metadata={}, raw=raw, unit="first", expected_session_ref=first_ref
    )
    second_receipt_matches = original_session_receipt_matches(
        metadata={}, raw=raw, unit="second", expected_session_ref=second_ref
    )
    first_principal = (
        _safe_reference(first.get("principal_ref")) if first_receipt_matches else None
    )
    second_principal = (
        _safe_reference(second.get("principal_ref")) if second_receipt_matches else None
    )
    first_digest = first.get("body_sha256")
    second_digest = second.get("body_sha256")
    if (
        not is_http_status(first_status)
        or not is_http_status(second_status)
        or not is_hex64(first_digest)
        or not is_hex64(second_digest)
        or output["same_status"] != (first_status == second_status)
        or output["same_body"] != (first_digest == second_digest)
    ):
        return GraphMutation()
    if (
        first_receipt_matches and second_receipt_matches
        and first_principal is not None and first_principal == second_principal
    ):
        return GraphMutation()

    host, port, path, target_ref = context
    endpoint_id = endpoint_node_id("GET", path, host=host, port=port)
    observation: dict[str, Any] = {
        "kind": "http_session_comparison",
        "context_key": _comparison_key("sessions", path, first_ref, second_ref),
        "method": "GET",
        "path": path,
        "target_ref": target_ref,
        "first_session_ref": first_ref,
        "second_session_ref": second_ref,
        "first_status": first_status,
        "second_status": second_status,
        "same_status": output["same_status"],
        "same_body": output["same_body"],
        "verdict": "observation_only",
    }
    if first_principal is not None:
        observation["first_principal_ref"] = first_principal
    if second_principal is not None:
        observation["second_principal_ref"] = second_principal
    raw_output = raw.get("output")
    if first_receipt_matches and isinstance(raw_output, dict):
        first_actual = raw_output.get("first")
        if isinstance(first_actual, dict) and "actual_session_ref" in first_actual:
            observation["first_actual_session_ref"] = first_actual["actual_session_ref"]
    if second_receipt_matches and isinstance(raw_output, dict):
        second_actual = raw_output.get("second")
        if isinstance(second_actual, dict) and "actual_session_ref" in second_actual:
            observation["second_actual_session_ref"] = second_actual["actual_session_ref"]
    observation["first_body_sha256"] = f"sha256:{first_digest.lower()}"
    observation["second_body_sha256"] = f"sha256:{second_digest.lower()}"

    first_nodes: list[NodeMutation] = []
    first_edges: list[EdgeMutation] = []
    if first_receipt_matches and first_principal is not None:
        first_nodes, first_edges = _session_identity_mutations(
            target_ref=target_ref, session_ref=first_ref, endpoint_id=endpoint_id,
            status=first_status, principal_ref=first_principal,
        )
    second_nodes: list[NodeMutation] = []
    second_edges: list[EdgeMutation] = []
    if second_receipt_matches and second_principal is not None:
        second_nodes, second_edges = _session_identity_mutations(
            target_ref=target_ref, session_ref=second_ref, endpoint_id=endpoint_id,
            status=second_status, principal_ref=second_principal,
        )
    endpoint_facts: dict[str, Any] = {
        "host": host,
        "path": path,
        "method": "GET",
        "observed_target": path,
        "target_ref": target_ref,
        "assessment_kind": "observation",
    }
    if port is not None:
        endpoint_facts["port"] = port
    return GraphMutation(
        nodes=[
            NodeMutation(
                node_type=GraphNodeType.ENDPOINT,
                node_id=endpoint_id,
                attrs={"session_comparison_observed": True},
                facts=endpoint_facts,
                labels=["observed-by:web.http_compare_sessions"],
                observations=[observation],
            ),
            *first_nodes,
            *second_nodes,
        ],
        edges=[*first_edges, *second_edges],
    )


def map_web_http_security_posture(
    *, target: AnyTarget, arguments: dict[str, Any], result: ToolResult
) -> GraphMutation:
    """Store fixed security-header/cookie flags without names, values or a verdict."""

    if not result.success or not isinstance(result.output, dict):
        return GraphMutation()
    output = result.output
    context = _endpoint_context(target, arguments, output)
    status = output.get("status_code")
    headers = output.get("headers_present")
    cookies = output.get("cookies")
    cookie_count = output.get("set_cookie_header_count")
    if (
        context is None
        or not is_http_status(status)
        or not isinstance(headers, dict)
        or set(headers) != _SECURITY_HEADERS
        or any(type(value) is not bool for value in headers.values())
        or not isinstance(cookies, list)
        or len(cookies) > 50
        or type(cookie_count) is not int
        or not 0 <= cookie_count <= 10_000
        or type(output.get("cache_control_no_store")) is not bool
        or type(output.get("content_type_nosniff")) is not bool
        or type(output.get("cookies_truncated")) is not bool
        or output.get("raw_headers_omitted") is not True
        or output.get("cookie_names_and_values_omitted") is not True
        or output.get("verdict") != "observation_only"
        or output.get("request_count") != 1
        or len(cookies) != min(cookie_count, 50)
        or output["cookies_truncated"] != (cookie_count > 50)
    ):
        return GraphMutation()

    safe_cookies: list[dict[str, Any]] = []
    for cookie in cookies:
        if not isinstance(cookie, dict):
            return GraphMutation()
        parse_status = cookie.get("parse_status")
        if parse_status == "unknown":
            if set(cookie) != {"parse_status"}:
                return GraphMutation()
            safe_cookies.append({"parse_status": "unknown"})
            continue
        if (
            parse_status != "parsed"
            or set(cookie)
            != {
                "parse_status",
                "secure",
                "http_only",
                "same_site",
                "domain_attribute_present",
                "persistent",
            }
            or cookie.get("same_site") not in {"strict", "lax", "none", "unspecified", "invalid"}
            or any(
                type(cookie.get(key)) is not bool
                for key in (
                    "secure",
                    "http_only",
                    "domain_attribute_present",
                    "persistent",
                )
            )
        ):
            return GraphMutation()
        safe_cookies.append(
            {
                "parse_status": "parsed",
                "secure": cookie["secure"],
                "http_only": cookie["http_only"],
                "same_site": cookie["same_site"],
                "domain_attribute_present": cookie["domain_attribute_present"],
                "persistent": cookie["persistent"],
            }
        )

    host, port, path, target_ref = context
    session_ref = arguments.get("session_ref")
    if session_ref is not None:
        session_ref = _safe_reference(session_ref)
        if session_ref is None:
            return GraphMutation()
    endpoint_id = endpoint_node_id("GET", path, host=host, port=port)
    observation = {
        "kind": "http_security_posture",
        "context_key": _comparison_key("security-posture", path, session_ref or "anonymous"),
        "method": "GET",
        "path": path,
        "target_ref": target_ref,
        "session_ref": session_ref,
        "status_code": status,
        "headers_present": dict(sorted(headers.items())),
        "cache_control_no_store": output["cache_control_no_store"],
        "content_type_nosniff": output["content_type_nosniff"],
        "cookies": safe_cookies,
        "set_cookie_header_count": cookie_count,
        "cookies_truncated": output["cookies_truncated"],
        "raw_headers_omitted": True,
        "cookie_names_and_values_omitted": True,
        "verdict": "observation_only",
    }
    if original_session_receipt_matches(
        metadata={}, raw=result.model_dump(mode="json"), unit="request",
        expected_session_ref=session_ref,
    ) and "actual_session_ref" in output:
        observation["actual_session_ref"] = output["actual_session_ref"]
    endpoint_facts: dict[str, Any] = {
        "host": host,
        "path": path,
        "method": "GET",
        "observed_target": path,
        "target_ref": target_ref,
        "assessment_kind": "observation",
    }
    if port is not None:
        endpoint_facts["port"] = port
    nodes = [
        NodeMutation(
            node_type=GraphNodeType.ENDPOINT,
            node_id=endpoint_id,
            attrs={"security_posture_observed": True, "status_code": status},
            facts=endpoint_facts,
            labels=["observed-by:web.http_security_posture"],
            observations=[observation],
        )
    ]
    edges: list[EdgeMutation] = []
    if session_ref is not None and "actual_session_ref" in observation:
        identity_nodes, identity_edges = _session_identity_mutations(
            target_ref=target_ref,
            session_ref=session_ref,
            endpoint_id=endpoint_id,
            status=status,
        )
        nodes.extend(identity_nodes)
        edges.extend(identity_edges)
    return GraphMutation(nodes=nodes, edges=edges)


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
    context = _endpoint_context(target, arguments, output)
    session_ref = _safe_reference(arguments.get("session_ref"))
    anonymous = output.get("anonymous")
    authenticated = output.get("authenticated")
    if (
        context is None
        or session_ref is None
        or not isinstance(anonymous, dict)
        or not isinstance(authenticated, dict)
        or type(output.get("same_status")) is not bool
        or type(output.get("same_body")) is not bool
        or output.get("verdict") != "observation_only"
        or output.get("request_count") != 2
    ):
        return GraphMutation()

    raw = result.model_dump(mode="json")
    anonymous_receipt_matches = original_session_receipt_matches(
        metadata={}, raw=raw, unit="anonymous", expected_session_ref=None
    )
    authenticated_receipt_matches = original_session_receipt_matches(
        metadata={}, raw=raw, unit="authenticated", expected_session_ref=session_ref
    )
    anonymous_status = anonymous.get("status_code")
    authenticated_status = authenticated.get("status_code")
    anonymous_digest = anonymous.get("body_sha256")
    authenticated_digest = authenticated.get("body_sha256")
    if (
        not is_http_status(anonymous_status)
        or not is_http_status(authenticated_status)
        or not is_hex64(anonymous_digest)
        or not is_hex64(authenticated_digest)
        or output["same_status"] != (anonymous_status == authenticated_status)
        or output["same_body"] != (anonymous_digest == authenticated_digest)
    ):
        return GraphMutation()

    host, port, path, target_ref = context
    endpoint_id = endpoint_node_id("GET", path, host=host, port=port)
    comparison = {
        "kind": "http_access_comparison",
        "context_key": request_key("GET", path, session_ref),
        "method": "GET",
        "path": path,
        "target_ref": target_ref,
        "session_ref": session_ref,
        "anonymous_status": anonymous_status,
        "authenticated_status": authenticated_status,
        "same_status": output["same_status"],
        "same_body": output["same_body"],
        "anonymous_body_sha256": f"sha256:{anonymous_digest.lower()}",
        "authenticated_body_sha256": f"sha256:{authenticated_digest.lower()}",
        "verdict": "observation_only",
    }
    raw_output = raw.get("output")
    if anonymous_receipt_matches and isinstance(raw_output, dict):
        anonymous_output = raw_output.get("anonymous")
        if isinstance(anonymous_output, dict) and "actual_session_ref" in anonymous_output:
            comparison["anonymous_actual_session_ref"] = anonymous_output["actual_session_ref"]
    if authenticated_receipt_matches and isinstance(raw_output, dict):
        authenticated_output = raw_output.get("authenticated")
        if isinstance(authenticated_output, dict) and "actual_session_ref" in authenticated_output:
            comparison["authenticated_actual_session_ref"] = authenticated_output[
                "actual_session_ref"
            ]
    identity_nodes: list[NodeMutation] = []
    identity_edges: list[EdgeMutation] = []
    if authenticated_receipt_matches:
        identity_nodes, identity_edges = _session_identity_mutations(
            target_ref=target_ref, session_ref=session_ref, endpoint_id=endpoint_id,
            status=authenticated_status,
        )
    return GraphMutation(
        nodes=[
            NodeMutation(
                node_type=GraphNodeType.ENDPOINT,
                node_id=endpoint_id,
                attrs={"web_inspected": True},
                facts={
                    "host": host,
                    "path": path,
                    "method": "GET",
                    "observed_target": path,
                    "target_ref": target_ref,
                    "assessment_kind": "observation",
                    **({"port": port} if port is not None else {}),
                },
                labels=["observed-by:web.http_compare_anonymous"],
                observations=[comparison],
            ),
            *identity_nodes,
        ],
        edges=identity_edges,
    )


def map_web_http_request(
    *, target: AnyTarget, arguments: dict[str, Any], result: ToolResult
) -> GraphMutation:
    if not result.success or not isinstance(result.output, dict):
        return GraphMutation()
    output = result.output
    context = _endpoint_context(target, arguments, output)
    path = arguments.get("path")
    method = arguments.get("method", "GET")
    status = output.get("status_code")
    redirect = output.get("redirect")
    argument_session_ref = arguments.get("session_ref")
    if (
        context is None
        or not isinstance(method, str)
        or method not in {"GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE"}
        or not is_http_status(status)
        or type(redirect) is not bool
        or redirect != (300 <= status < 400)
        or (
            argument_session_ref is not None
            and _safe_reference(argument_session_ref) is None
        )
    ):
        return GraphMutation()

    host, port, path, target_ref = context
    facts: dict[str, Any] = {
        "host": host,
        "path": path,
        "method": method,
        "observed_target": path,
        "target_ref": target_ref,
        "assessment_kind": "observation",
    }
    parameters = path_parameters(path)
    if parameters:
        facts["path_parameters"] = parameters
    headers = output.get("security_headers_present")
    digest = output.get("body_sha256")
    if (
        not isinstance(headers, dict)
        or set(headers)
        - {
            "strict-transport-security",
            "content-security-policy",
            "x-content-type-options",
            "cache-control",
        }
        or any(type(value) is not bool for value in headers.values())
        or not is_hex64(digest)
        or not (
            output.get("body_omitted") is True
            or (output.get("body_omitted") is False
                and isinstance(output.get("body_preview"), str)
                and output.get("body_preview_is_untrusted_data") is True
                and type(output.get("body_preview_truncated")) is bool)
        )
        or type(output.get("body_bytes")) is not int
        or output["body_bytes"] < 0
    ):
        return GraphMutation()
    if port is not None:
        facts["port"] = port
    observation = http_observation(arguments, result)
    if isinstance(observation, dict):
        observation["body_sha256"] = f"sha256:{digest.lower()}"
        observation.pop("actual_session_ref", None)
        if original_session_receipt_matches(
            metadata={}, raw=result.model_dump(mode="json"), unit="request",
            expected_session_ref=argument_session_ref,
        ) and "actual_session_ref" in output:
            observation["actual_session_ref"] = output["actual_session_ref"]
        for metadata_key in ("redirect_metadata", "auth_challenge", "cookie_metadata"):
            if metadata_key in observation:
                facts[metadata_key] = observation[metadata_key]
    facts["security_headers_present"] = dict(headers)
    endpoint_id = endpoint_node_id(method, path, host=host, port=port)
    nodes = [
        NodeMutation(
                node_type=GraphNodeType.ENDPOINT,
                node_id=endpoint_id,
                attrs={
                    "web_inspected": True,
                    "status_code": status,
                    "redirect": redirect,
                    **({"object_scoped": True} if parameters else {}),
                },
                facts=facts,
                labels=["observed-by:web.http_request"],
                observations=[observation] if observation else [],
            )
    ]
    edges: list[EdgeMutation] = []
    source_id = node_id_for_target(target)
    if isinstance(target, HostTarget) and source_id != endpoint_id:
        # The executed request measured this resource on its source target.
        # Preserve that relation with the request's evidence provenance so
        # later claims can consume host and endpoint originals together.
        edges.append(EdgeMutation(
            GraphEdgeType.HOSTS, source_id, endpoint_id,
            {"relation": {
                "type": GraphEdgeType.HOSTS.value,
                "source_node_id": str(source_id),
                "target_node_id": str(endpoint_id),
            }},
        ))
    if (
        isinstance(argument_session_ref, str)
        and isinstance(observation, dict)
        and "actual_session_ref" in observation
    ):
        identity_nodes, identity_edges = _session_identity_mutations(
            target_ref=target_ref,
            session_ref=argument_session_ref,
            endpoint_id=endpoint_id,
            status=status,
        )
        nodes.extend(identity_nodes)
        edges.extend(identity_edges)
    return GraphMutation(nodes=nodes, edges=edges)


_SESSION_LIST_LIMIT = 100


def map_web_session_list(
    *, target: AnyTarget, arguments: dict[str, Any], result: ToolResult
) -> GraphMutation:
    """Record operator-provisioned session bindings as identities.

    Real output shape (verified directly against
    ``redteam.capability.servers.web_http.session_list``, the only place
    this tool's ``output`` dict is built)::

        {"sessions": [{"session_ref": str, "target_ref": str, "principal_ref": str}, ...]}

    Each returned item becomes a PRINCIPAL/CREDENTIAL pair with the same
    ``session_for`` identity relationship the HTTP mappers use -- built
    with the existing ``session_*`` identity helpers, never a new schema.
    No ``CAN_ACCESS`` edge is created: this tool reports which test
    sessions the backend provisioned, not an authentication success. That
    distinction is carried by ``binding_kind``/``identity_basis`` alone.
    Only actually returned items are processed; an empty list yields an
    empty mutation, and only references are stored, never secret values.
    """

    if not result.success or not isinstance(result.output, dict):
        return GraphMutation()
    raw_sessions = result.output.get("sessions")
    if not isinstance(raw_sessions, list) or len(raw_sessions) > _SESSION_LIST_LIMIT:
        return GraphMutation()

    bindings: dict[tuple[str, str], str | None] = {}
    conflicted: set[tuple[str, str]] = set()
    for item in raw_sessions:
        if not isinstance(item, dict):
            continue
        target_ref = _safe_reference(item.get("target_ref"))
        session_ref = _safe_reference(item.get("session_ref"))
        if target_ref is None or session_ref is None:
            continue
        key = (target_ref, session_ref)
        if key in conflicted:
            continue
        principal = _safe_reference(item.get("principal_ref"))
        if key in bindings:
            if bindings[key] != principal:
                del bindings[key]
                conflicted.add(key)
        else:
            bindings[key] = principal

    nodes: list[NodeMutation] = []
    edges: list[EdgeMutation] = []
    for (target_ref, session_ref), principal in bindings.items():
        principal_id = session_principal_node_id(target_ref, session_ref)
        credential_id = session_credential_node_id(target_ref, session_ref)
        principal_facts: dict[str, Any] = {
            "target_ref": target_ref,
            "session_refs": [session_ref],
        }
        observation: dict[str, Any] = {
            "kind": "provisioned_session_binding",
            "context_key": deterministic_key(
                "provisioned-session-binding", target_ref, session_ref
            ),
            "source_tool": "web.session_list",
            "target_ref": target_ref,
            "session_ref": session_ref,
            "binding_kind": "provisioned",
            "identity_basis": "provisioned_binding",
            "verdict": "observation_only",
        }
        if principal is not None:
            principal_facts["principal_ref"] = principal
            observation["principal_ref"] = principal
        nodes.append(
            NodeMutation(
                node_type=GraphNodeType.PRINCIPAL,
                node_id=principal_id,
                attrs={
                    "operator_managed_session": True,
                    "provisioned_binding": True,
                },
                facts=principal_facts,
                labels=["observed-session-principal", "observed-by:web.session_list"],
                observations=[observation],
            )
        )
        nodes.append(
            NodeMutation(
                node_type=GraphNodeType.CREDENTIAL,
                node_id=credential_id,
                attrs={"reference_only": True, "provisioned_binding": True},
                facts={
                    "target_ref": target_ref,
                    "session_ref": session_ref,
                    "credential_kind": "session_reference",
                },
                labels=[
                    "operator-managed-session-reference",
                    "observed-by:web.session_list",
                ],
            )
        )
        edges.append(
            EdgeMutation(
                GraphEdgeType.RELATED_TO,
                credential_id,
                principal_id,
                {
                    "relation": "session_for",
                    "binding_kind": "provisioned",
                    "identity_basis": "provisioned_binding",
                },
            )
        )
    return GraphMutation(nodes=nodes, edges=edges)
