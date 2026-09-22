"""Attack Graph node ids: ``"<type>:<key>"`` construction and parsing.

Owned by Agent Team. Rationale: ``docs/agent/attack-graph.md``.
"""

from __future__ import annotations

import re
from urllib.parse import quote

from redteam.core.contracts.target import (
    AnyTarget,
    AssetTarget,
    DomainTarget,
    EndpointTarget,
    GraphNodeTarget,
    HostTarget,
    PrincipalTarget,
    ServiceTarget,
    UrlTarget,
    target_graph_node_id,
)
from redteam.core.enums import GraphNodeType
from redteam.core.ids import AssetId, GraphNodeId, PrincipalId

#: A path segment that looks like a generated identifier (``8831``,
#: ``7a3f21``, ``2024-0009``) rather than a route name.
_ID_SEGMENT = re.compile(
    r"^[0-9]+$"  # 8831
    r"|^(?=[0-9A-Fa-f-]*[0-9])[0-9A-Fa-f-]{8,}$"  # 7a3f21b0, 2024-0009 (must contain a digit)
    r"|^[0-9][A-Za-z0-9_-]*[0-9]$"  # 1abc9
)

_SEPARATOR = ":"


class InvalidNodeIdError(ValueError):
    """Raised when a node id does not follow the ``<type>:<key>`` convention."""


def normalize_path(path: str) -> str:
    """``/orders/8831/items?x=1`` -> ``/orders/{id}/items``."""
    path = path.split("?", 1)[0].split("#", 1)[0]
    segments = [s for s in path.split("/") if s]
    normalized = ["{id}" if _ID_SEGMENT.match(s) else s for s in segments]
    return "/" + "/".join(normalized)


def path_parameters(path: str) -> list[str]:
    """Object-identifier positions a path templates over, named after the
    collection: ``/orders/8831/items/42`` -> ``["orders_id", "items_id"]``."""
    segments = [s for s in normalize_path(path).split("/") if s]
    params: list[str] = []
    for index, segment in enumerate(segments):
        if segment == "{id}":
            collection = segments[index - 1] if index > 0 else "root"
            params.append(f"{collection}_id")
    return params


def make_node_id(node_type: GraphNodeType, key: str) -> GraphNodeId:
    """Build ``"<type>:<key>"``. ``key`` must be non-blank and printable."""
    if not key or not key.strip():
        raise InvalidNodeIdError(f"blank key for node type {node_type.value!r}")
    # Control characters corrupt log lines, the JSONL snapshot, and events.
    if any(ch < " " or ch == "\x7f" for ch in key):
        raise InvalidNodeIdError(
            f"key {key!r} contains a control character — ids must stay greppable"
        )
    return GraphNodeId(f"{node_type.value}{_SEPARATOR}{key}")


def split_node_id(node_id: GraphNodeId) -> tuple[GraphNodeType, str]:
    """Inverse of :func:`make_node_id`."""
    raw_type, separator, key = str(node_id).partition(_SEPARATOR)
    if not separator or not key:
        raise InvalidNodeIdError(f"node id {node_id!r} is not '<type>:<key>'")
    try:
        return GraphNodeType(raw_type), key
    except ValueError as exc:
        raise InvalidNodeIdError(f"node id {node_id!r} has unknown type {raw_type!r}") from exc


def is_well_formed(node_id: GraphNodeId) -> bool:
    try:
        split_node_id(node_id)
    except InvalidNodeIdError:
        return False
    return True


def host_node_id(host: str) -> GraphNodeId:
    return make_node_id(GraphNodeType.HOST, host.strip().lower())


def domain_node_id(domain: str) -> GraphNodeId:
    # One node whether first seen as a host or as a domain.
    return make_node_id(GraphNodeType.HOST, domain.strip().lower())


def endpoint_node_id(
    method: str, path: str, host: str | None = None, port: int | None = None
) -> GraphNodeId:
    """``"endpoint:a.local:8443 GET /orders/{id}"``. The port is part of the
    identity — ``:80`` and ``:8443`` are different services."""
    authority = ""
    if host:
        authority = host.strip().lower()
        if port is not None:
            authority = f"{authority}:{port}"
        authority += " "
    return make_node_id(
        GraphNodeType.ENDPOINT, f"{authority}{method.upper()} {normalize_path(path)}"
    )


def service_node_id(service_name: str, host: str | None = None) -> GraphNodeId:
    key = f"{host.strip().lower()}/{service_name}" if host else service_name
    return make_node_id(GraphNodeType.SERVICE, key)


def principal_node_id(principal: str) -> GraphNodeId:
    return make_node_id(GraphNodeType.PRINCIPAL, principal)


def session_principal_node_id(target_ref: str, session_ref: str) -> GraphNodeId:
    """A session identity scoped to the configured target it belongs to.

    Session names such as ``alice`` are commonly reused by separate MCP
    targets.  A global ``principal:session:alice`` id would merge unrelated
    identities and invent cross-target access relationships.
    """

    return principal_node_id(f"{target_ref}/session:{session_ref}")


def credential_node_id(credential_ref: str) -> GraphNodeId:
    """``credential_ref`` is a *reference*, never the secret itself."""
    return make_node_id(GraphNodeType.CREDENTIAL, credential_ref)


def session_credential_node_id(target_ref: str, session_ref: str) -> GraphNodeId:
    """Stable id for an operator-managed session reference, never its secret."""

    return credential_node_id(f"{target_ref}/session:{session_ref}")


def api_resource_node_id(
    host: str, path: str, port: int | None = None
) -> GraphNodeId:
    """Identity of an API resource template independent of its HTTP methods."""

    authority = host.strip().lower()
    if port is not None:
        authority = f"{authority}:{port}"
    return make_node_id(
        GraphNodeType.RESOURCE,
        f"{authority} {normalize_path(path)}",
    )


def flow_resource_node_id(scope: str, flow_ref: str) -> GraphNodeId:
    """A captured HTTP flow scoped to its configured target or target node."""

    return make_node_id(
        GraphNodeType.RESOURCE,
        f"{quote(scope, safe='')}/flow:{quote(flow_ref, safe='')}",
    )


def asset_node_id(asset: str) -> GraphNodeId:
    return make_node_id(GraphNodeType.ASSET, asset)


def hypothesis_node_id(hypothesis_id: str) -> GraphNodeId:
    return make_node_id(GraphNodeType.HYPOTHESIS, hypothesis_id)


def finding_node_id(finding_id: str) -> GraphNodeId:
    return make_node_id(GraphNodeType.FINDING, finding_id)


def _url_key(url: str) -> str:
    scheme, separator, rest = url.partition("://")
    if not separator:
        scheme, rest = "http", url
    authority, _, path = rest.partition("/")
    return f"{scheme}://{authority.lower()}{normalize_path('/' + path)}"


def node_id_for_target(target: AnyTarget) -> GraphNodeId:
    """The node a ``TargetRef`` refers to: its ``graph_node_id`` if set,
    otherwise derived from its identity. Every target kind resolves."""
    explicit = target_graph_node_id(target)
    if explicit is not None:
        return explicit

    if isinstance(target, HostTarget):
        return host_node_id(target.host)
    if isinstance(target, DomainTarget):
        return domain_node_id(target.domain)
    if isinstance(target, UrlTarget):
        return make_node_id(GraphNodeType.ENDPOINT, _url_key(target.url))
    if isinstance(target, EndpointTarget):
        return endpoint_node_id("ANY", target.path or "/", host=target.host, port=target.port)
    if isinstance(target, ServiceTarget):
        return service_node_id(target.service_name, target.host)
    if isinstance(target, PrincipalTarget):
        return principal_node_id(str(target.principal_id))
    if isinstance(target, AssetTarget):
        return asset_node_id(str(target.asset_id))
    if isinstance(target, GraphNodeTarget):  # pragma: no cover - covered by `explicit`
        return target.node_id
    raise TypeError(f"unknown target kind: {target!r}")  # pragma: no cover


def node_type_for_target(target: AnyTarget) -> GraphNodeType:
    """The ``GraphNodeType`` a ``TargetRef`` maps onto."""
    if isinstance(target, (HostTarget, DomainTarget)):
        return GraphNodeType.HOST
    if isinstance(target, (UrlTarget, EndpointTarget)):
        return GraphNodeType.ENDPOINT
    if isinstance(target, ServiceTarget):
        return GraphNodeType.SERVICE
    if isinstance(target, PrincipalTarget):
        return GraphNodeType.PRINCIPAL
    if isinstance(target, AssetTarget):
        return GraphNodeType.ASSET
    if isinstance(target, GraphNodeTarget):
        return target.node_type or GraphNodeType.RESOURCE
    raise TypeError(f"unknown target kind: {target!r}")  # pragma: no cover


#: Fact key holding the literal path/URL as observed, before
#: :func:`normalize_path` collapsed its identifier segments.
OBSERVED_TARGET_FACT = "observed_target"


def target_for_node(
    node_id: GraphNodeId,
    node_type: GraphNodeType,
    *,
    observed: str | None = None,
) -> AnyTarget:
    """Inverse of :func:`node_id_for_target`. Returns a concrete,
    scope-checkable target — scope checking cannot match a ``GraphNodeTarget``.

    Node ids normalise identifier segments (``/orders/8831`` ->
    ``/orders/{id}``) so one endpoint stays one node. That template is an
    identity, not an address: acting on it would request ``/orders/{id}``
    literally. Pass ``observed`` — the concrete path/URL the node was seen
    at — to target the real thing.
    """
    _, key = split_node_id(node_id)

    if node_type is GraphNodeType.HOST:
        return HostTarget(host=key, graph_node_id=node_id)
    if node_type is GraphNodeType.SERVICE:
        host, separator, service_name = key.partition("/")
        if separator:
            return ServiceTarget(service_name=service_name, host=host, graph_node_id=node_id)
        return ServiceTarget(service_name=key, graph_node_id=node_id)
    if node_type is GraphNodeType.ENDPOINT:
        if "://" in key:
            return UrlTarget(url=observed or key, graph_node_id=node_id)
        parts = key.split(" ")
        if len(parts) >= 3:
            # "<host>[:<port>] <METHOD> /path" — the method is not part of
            # the target.
            authority, port = parts[0], None
            host, separator, tail = authority.rpartition(":")
            if separator and tail.isdigit():
                authority, port = host, int(tail)
            return EndpointTarget(
                host=authority,
                port=port,
                path=observed or " ".join(parts[2:]),
                graph_node_id=node_id,
            )
        # No host: nothing to scope-check, and saying so beats inventing one.
        return UrlTarget(url=observed or parts[-1], graph_node_id=node_id)
    if node_type is GraphNodeType.PRINCIPAL:
        return PrincipalTarget(principal_id=PrincipalId(key), display_name=key)
    if node_type is GraphNodeType.ASSET:
        return AssetTarget(asset_id=AssetId(key), display_name=key)

    return GraphNodeTarget(node_id=node_id, node_type=node_type, display_name=key)
