"""``TargetRef`` — the common way Action/Evidence/Graph point at a target.

A discriminated union (not a flat ``dict[str, Any]``) so each kind
carries exactly the fields that make sense for it, and callers can
``match``/``isinstance`` on the concrete variant instead of guessing
which loosely-typed fields are populated.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field

from redteam.core.enums import GraphNodeType
from redteam.core.ids import AssetId, GraphNodeId, PrincipalId


class HostTarget(BaseModel):
    kind: Literal["host"] = "host"
    host: str
    graph_node_id: GraphNodeId | None = None


class DomainTarget(BaseModel):
    kind: Literal["domain"] = "domain"
    domain: str
    graph_node_id: GraphNodeId | None = None


class UrlTarget(BaseModel):
    kind: Literal["url"] = "url"
    url: str
    graph_node_id: GraphNodeId | None = None


class EndpointTarget(BaseModel):
    kind: Literal["endpoint"] = "endpoint"
    host: str
    port: int | None = None
    path: str | None = None
    graph_node_id: GraphNodeId | None = None


class ServiceTarget(BaseModel):
    kind: Literal["service"] = "service"
    service_name: str
    host: str | None = None
    graph_node_id: GraphNodeId | None = None


class PrincipalTarget(BaseModel):
    kind: Literal["principal"] = "principal"
    principal_id: PrincipalId
    display_name: str | None = None


class AssetTarget(BaseModel):
    kind: Literal["asset"] = "asset"
    asset_id: AssetId
    display_name: str | None = None


class GraphNodeTarget(BaseModel):
    """Escape hatch: point directly at an existing Attack Graph node
    when none of the other kinds fit (or the node type is one this
    union hasn't grown a dedicated variant for yet)."""

    kind: Literal["graph_node"] = "graph_node"
    node_id: GraphNodeId
    node_type: GraphNodeType | None = None
    display_name: str | None = None


AnyTarget = (
    HostTarget
    | DomainTarget
    | UrlTarget
    | EndpointTarget
    | ServiceTarget
    | PrincipalTarget
    | AssetTarget
    | GraphNodeTarget
)

TargetRef = Annotated[AnyTarget, Field(discriminator="kind")]
"""Use this as the field annotation on a Pydantic model (needs the
discriminator). Use ``AnyTarget`` for plain function signatures."""


def target_graph_node_id(target: AnyTarget) -> GraphNodeId | None:
    """The Attack Graph node this target resolves to, if known yet."""
    if isinstance(target, GraphNodeTarget):
        return target.node_id
    return getattr(target, "graph_node_id", None)


def target_display_name(target: AnyTarget) -> str:
    """Human-readable label for logs/UI — not a stable identifier."""
    if isinstance(target, HostTarget):
        return target.host
    if isinstance(target, DomainTarget):
        return target.domain
    if isinstance(target, UrlTarget):
        return target.url
    if isinstance(target, EndpointTarget):
        return f"{target.host}:{target.port}" if target.port else target.host
    if isinstance(target, ServiceTarget):
        return target.service_name
    if isinstance(target, PrincipalTarget):
        return target.display_name or str(target.principal_id)
    if isinstance(target, AssetTarget):
        return target.display_name or str(target.asset_id)
    if isinstance(target, GraphNodeTarget):
        return target.display_name or str(target.node_id)
    raise TypeError(f"unknown target kind: {target!r}")  # pragma: no cover
