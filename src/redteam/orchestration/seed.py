"""Plants the engagement scope as the first Attack Graph nodes.

An empty graph is an empty frontier. Seeding makes a target *visible* to a
planner; the Control Plane still decides whether acting on it is allowed.
"""

from __future__ import annotations

from collections.abc import Mapping

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
from redteam.core.provider_contracts import ExecutionRecipeSpec
from redteam.orchestration.authentication import AuthSession
from redteam.orchestration.execution_bridge import execution_facts_by_asset

SCOPE_LABEL = "engagement-scope"


def _asset_ref_map(
    engagement: Engagement,
    assets: list[str],
    *,
    singular: str,
    plural: str,
) -> dict[str, str]:
    """Validate scalar/map metadata forms and return refs by asset id."""

    raw = engagement.metadata.get(plural, {})
    if not isinstance(raw, dict) or any(
        not isinstance(asset, str)
        or not asset.strip()
        or not isinstance(ref, str)
        or not ref.strip()
        for asset, ref in raw.items()
    ):
        raise ValueError(
            f"engagement.metadata.{plural} must map assets to non-empty MCP policy refs"
        )
    refs = {asset.strip(): ref.strip() for asset, ref in raw.items()}

    scalar = engagement.metadata.get(singular)
    if scalar is None:
        return refs
    if not isinstance(scalar, str) or not scalar.strip():
        raise ValueError(f"engagement.metadata.{singular} must be a non-empty MCP policy ref")
    if len(assets) != 1:
        raise ValueError(
            f"engagement.metadata.{singular} is only valid with exactly one allowed asset"
        )
    refs[assets[0]] = scalar.strip()
    return refs


async def seed_from_engagement(
    service: AttackGraphService,
    engagement: Engagement,
    *,
    run_id: RunId | None = None,
    step: int = 0,
    execution_recipes: Mapping[str, ExecutionRecipeSpec] | None = None,
) -> list[GraphNode]:
    """Create one in-scope node per allowed host/domain and mobile asset."""
    denied = {
        host.lower() for host in (*engagement.scope.denied_hosts, *engagement.scope.denied_domains)
    }
    declared = [
        value
        for value in (*engagement.scope.allowed_hosts, *engagement.scope.allowed_domains)
        if value and value.lower() not in denied
    ]
    denied_assets = {asset.strip() for asset in engagement.scope.denied_assets if asset.strip()}
    declared_assets = list(
        dict.fromkeys(
            asset.strip()
            for asset in engagement.scope.allowed_assets
            if asset.strip() and asset.strip() not in denied_assets
        )
    )

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
    asset_refs = _asset_ref_map(
        engagement,
        declared_assets,
        singular="asset_ref",
        plural="asset_refs",
    )
    apk_refs = _asset_ref_map(
        engagement,
        declared_assets,
        singular="apk_ref",
        plural="apk_refs",
    )
    device_refs = _asset_ref_map(
        engagement,
        declared_assets,
        singular="device_ref",
        plural="device_refs",
    )
    device_serials = _asset_ref_map(
        engagement,
        declared_assets,
        singular="device_serial",
        plural="device_serials",
    )
    package_names = _asset_ref_map(
        engagement,
        declared_assets,
        singular="package_name",
        plural="package_names",
    )
    execution_facts = execution_facts_by_asset(
        engagement, declared_assets, recipes=execution_recipes
    )
    for asset in declared_assets:
        configured = {
            asset in device_serials,
            asset in device_refs,
            asset in package_names,
        }
        if len(configured) != 1:
            raise ValueError(
                "engagement metadata must supply device_serial, device_ref, and "
                "package_name together "
                f"for asset {asset!r}"
            )

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
                    # Top-level list claims retain provenance for each element
                    # when hosts are shared by multiple runs.
                    "auth_session_registrations": [
                        {
                            "session_ref": item["session_ref"],
                            "run_id": str(run_id) if run_id else None,
                        }
                        for item in auth_sessions.get(value, [])
                    ],
                },
                labels=[SCOPE_LABEL],
                provenance=provenance,
                step=step,
            )
        )
    for asset in declared_assets:
        asset_ref = asset_refs.get(asset, asset)
        apk_ref = apk_refs.get(asset)
        asset_facts = {
            "asset_ref": asset_ref,
            # Generic Asset tools still need a stable operator-owned target
            # alias.  APK identity is a separate, explicit declaration.
            "target_ref": apk_ref or asset_ref,
            **execution_facts.get(asset, {}),
        }
        if apk_ref is not None:
            asset_facts["apk_ref"] = apk_ref
        if asset in device_refs:
            asset_facts.update(
                device_serial=device_serials[asset],
                device_ref=device_refs[asset],
                package_name=package_names[asset],
            )
        seeded.append(
            await service.upsert_node(
                GraphNodeType.ASSET,
                key=asset,
                attrs={"in_scope": True},
                facts=asset_facts,
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
