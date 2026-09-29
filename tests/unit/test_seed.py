from __future__ import annotations

from datetime import datetime, timezone

import pytest

from redteam.attack_graph.observation import GraphObservation
from redteam.attack_graph.patterns import NodePattern
from redteam.attack_graph.predicates import facts_of
from redteam.attack_graph.service import AttackGraphService
from redteam.attack_graph.stores.memory import InMemoryAttackGraphStore
from redteam.core.contracts.engagement import (
    ApprovalPolicy,
    Engagement,
    EngagementScope,
    ExecutionBudget,
    RateLimitPolicy,
)
from redteam.core.enums import GraphNodeType
from redteam.core.ids import AgentId, EngagementId, ToolId
from redteam.orchestration.agents.base import AgentSpec, PlanStep, available_steps
from redteam.orchestration.seed import SCOPE_LABEL, seed_from_engagement


def engagement(
    *,
    allowed_assets: list[str],
    denied_assets: list[str] | None = None,
    metadata: dict | None = None,
) -> Engagement:
    return Engagement(
        engagement_id=EngagementId("eng_assets"),
        name="asset seed",
        scope=EngagementScope(
            allowed_assets=allowed_assets,
            denied_assets=denied_assets or [],
        ),
        execution_budget=ExecutionBudget(max_actions=10),
        rate_limit_policy=RateLimitPolicy(max_actions_per_minute=10),
        approval_policy=ApprovalPolicy(),
        created_at=datetime.now(timezone.utc),
        metadata=metadata or {},
    )


async def test_allowed_assets_are_seeded_with_policy_refs_and_denies_win() -> None:
    service = AttackGraphService(InMemoryAttackGraphStore())

    seeded = await seed_from_engagement(
        service,
        engagement(
            allowed_assets=["bank-apk", "skip-apk", "bank-apk"],
            denied_assets=["skip-apk"],
            metadata={
                "asset_refs": {"bank-apk": "artifact-policy-key"},
                "apk_refs": {"bank-apk": "apk-policy-key"},
            },
        ),
    )

    assets = [node for node in seeded if node.type is GraphNodeType.ASSET]
    assert [str(node.id) for node in assets] == ["asset:bank-apk"]
    assert assets[0].labels == [SCOPE_LABEL]
    assert facts_of(assets[0].properties) == {
        "asset_ref": "artifact-policy-key",
        "apk_ref": "apk-policy-key",
        "target_ref": "apk-policy-key",
    }


async def test_single_asset_accepts_scalar_policy_refs() -> None:
    service = AttackGraphService(InMemoryAttackGraphStore())

    [asset] = await seed_from_engagement(
        service,
        engagement(
            allowed_assets=["insecurebankv2"],
            metadata={"asset_ref": "lab-artifact", "apk_ref": "analysis-policy-key"},
        ),
    )

    assert facts_of(asset.properties)["target_ref"] == "analysis-policy-key"


@pytest.mark.parametrize(
    "metadata",
    [
        {"apk_ref": "one-key"},
        {"asset_ref": "one-key"},
        {"apk_refs": {"bank-apk": ""}},
        {"asset_refs": []},
    ],
)
async def test_invalid_or_ambiguous_asset_policy_refs_are_rejected(metadata: dict) -> None:
    service = AttackGraphService(InMemoryAttackGraphStore())

    with pytest.raises(ValueError, match="engagement.metadata"):
        await seed_from_engagement(
            service,
            engagement(allowed_assets=["one", "two"], metadata=metadata),
        )


async def test_asset_target_ref_fact_resolves_playbook_placeholder() -> None:
    service = AttackGraphService(InMemoryAttackGraphStore())
    await seed_from_engagement(
        service,
        engagement(
            allowed_assets=["bank-apk"],
            metadata={"apk_ref": "analysis-policy-key"},
        ),
    )
    spec = AgentSpec(
        agent_id=AgentId("analysis-test"),
        name="analysis test",
        capabilities=["analysis"],
        playbook=[
            PlanStep(
                step_id="list-apk",
                capability="analysis",
                operation="static_analysis",
                tool_id=ToolId("analysis.apk_file_listing"),
                target_bind="asset",
                preconditions=[
                    NodePattern(type=GraphNodeType.ASSET, bind="asset", from_param="asset")
                ],
                arguments={"apk_ref": "$target_ref"},
                reason="Inspect the authorized APK",
            )
        ],
    )

    [step] = await available_steps(
        spec,
        service.view(),
        GraphObservation(),
        step=1,
    )

    assert step.arguments == {"apk_ref": "analysis-policy-key"}
