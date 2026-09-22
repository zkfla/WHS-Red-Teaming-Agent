"""Plants the engagement scope as the first Attack Graph nodes.

An empty graph is an empty frontier. Seeding makes a host *visible* to a
planner; the Control Plane still decides whether acting on it is allowed.
"""

from __future__ import annotations

from redteam.attack_graph.predicates import Confidence, Provenance
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
        host.lower()
        for host in (*engagement.scope.denied_hosts, *engagement.scope.denied_domains)
    }
    declared = [
        value
        for value in (*engagement.scope.allowed_hosts, *engagement.scope.allowed_domains)
        if value and value.lower() not in denied
    ]

    target_refs = engagement.metadata.get("target_refs", {})
    if not isinstance(target_refs, dict) or any(
        not isinstance(host, str) or not host.strip()
        or not isinstance(ref, str) or not ref.strip()
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
                facts={"target_ref": target_refs.get(value, value),
                       "auth_session_config": {"sessions": auth_sessions.get(value, [])}},
                labels=[SCOPE_LABEL],
                provenance=provenance,
                step=step,
            )
        )
    return seeded
