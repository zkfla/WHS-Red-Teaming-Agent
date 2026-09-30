"""Plants the engagement scope as the first Attack Graph nodes.

An empty graph is an empty frontier. Seeding makes a host *visible* to a
planner; the Control Plane still decides whether acting on it is allowed.
"""

from __future__ import annotations

from redteam.attack_graph.predicates import (
    Confidence,
    Provenance,
    attrs_of,
    facts_of,
    provenance_of,
)
from redteam.attack_graph.service import AttackGraphService
from redteam.core.contracts.engagement import Engagement
from redteam.core.contracts.graph import GraphNode
from redteam.core.enums import GraphNodeType
from redteam.core.ids import RunId
from redteam.orchestration.authentication import AuthSession

SCOPE_LABEL = "engagement-scope"


async def seed_from_engagement(
    service: AttackGraphService,
    engagement: Engagement,
    *,
    run_id: RunId | None = None,
    step: int = 0,
) -> list[GraphNode]:
    """Create one in-scope Host node per allowed host/domain."""
    denied = {
        host.lower() for host in (*engagement.scope.denied_hosts, *engagement.scope.denied_domains)
    }
    declared = [
        value
        for value in (*engagement.scope.allowed_hosts, *engagement.scope.allowed_domains)
        if value and value.lower() not in denied
    ]

    target_refs = engagement.metadata.get("target_refs", {})
    if not isinstance(target_refs, dict) or any(
        not isinstance(host, str) or not host.strip() or not isinstance(ref, str) or not ref.strip()
        for host, ref in target_refs.items()
    ):
        raise ValueError(
            "engagement.metadata.target_refs must map hosts to non-empty MCP target refs"
        )
    target_refs = {host.strip().lower(): ref for host, ref in target_refs.items()}
    raw_sessions = engagement.metadata.get("auth_sessions", {})
    if not isinstance(raw_sessions, dict) or any(
        not isinstance(host, str) or not isinstance(items, list)
        for host, items in raw_sessions.items()
    ):
        raise ValueError("engagement.metadata.auth_sessions must map hosts to session lists")
    auth_sessions = {
        host.strip().lower(): [AuthSession.model_validate(item).model_dump() for item in items]
        for host, items in raw_sessions.items()
    }

    provenance = Provenance(
        step=step,
        source=f"engagement:{engagement.engagement_id}",
        run_id=str(run_id) if run_id else None,
        # The engagement document is an authoritative declaration of
        # scope, not something we measured — but it is not a guess either.
        confidence=Confidence.SERVER_DECLARED,
    )

    seeded: list[GraphNode] = []
    for value in dict.fromkeys(host.strip().lower() for host in declared):
        seeded.append(
            await service.upsert_node(
                GraphNodeType.HOST,
                key=value,
                attrs={"in_scope": True},
                facts={
                    "target_ref": target_refs.get(value, value),
                    "auth_session_config": {"sessions": auth_sessions.get(value, [])},
                },
                labels=[SCOPE_LABEL],
                provenance=provenance,
                step=step,
            )
        )
    return seeded


def is_provided_node(node: GraphNode) -> bool:
    if SCOPE_LABEL in list(node.labels or []):
        return True
    attrs = attrs_of(node.properties or {})
    if attrs.get("in_scope") is True:
        for entry in provenance_of(node.properties or {}):
            if not isinstance(entry, dict):
                continue
            if str(entry.get("confidence", "")) == "server_declared":
                return True
            if str(entry.get("source", "")).startswith("engagement:"):
                return True
    return False


def provided_target_ref(node: GraphNode) -> str | None:
    ref = facts_of(node.properties or {}).get("target_ref", None)
    return ref if isinstance(ref, str) and ref else None


def provided_auth_session_refs(node: GraphNode) -> list[str]:
    cfg = facts_of(node.properties or {}).get("auth_session_config", {})
    if not isinstance(cfg, dict):
        return []
    raw = cfg.get("sessions", [])
    if not isinstance(raw, list):
        return []
    refs: list[str] = []
    for item in raw:
        if isinstance(item, dict) and item.get("session_ref") is not None:
            refs.append(str(item.get("session_ref")))
        elif isinstance(item, str) and item:
            refs.append(item)
    return sorted(set(refs))


async def list_provided_nodes(service: AttackGraphService) -> list[GraphNode]:
    try:
        nodes = await service.list_nodes()
    except Exception:
        return []
    provided = [n for n in (nodes or []) if is_provided_node(n)]
    provided.sort(key=lambda n: str(n.id))
    return provided
