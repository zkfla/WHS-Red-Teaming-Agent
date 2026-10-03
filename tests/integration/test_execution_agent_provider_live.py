"""Opt-in real-provider proof for the fixed execution Agent bridge."""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

import pytest

from redteam.app.bootstrap import build_llm_client, run_engagement
from redteam.control.risk import RegistryRiskResolver
from redteam.control.service import DefaultControlPlane
from redteam.core.config import LLMConfig
from redteam.core.enums import EvidenceSourceType
from redteam.orchestration.settings import OrchestrationSettings
from tests.fakes import build_test_application
from tests.integration.test_execution_agent_e2e import (
    RUN_ID,
    engagement,
    gateway_for,
    policy,
)


def _provider_skip_reason() -> str | None:
    if os.environ.get("REDTEAM_TEST_EXECUTION_LLM") != "1":
        return "set REDTEAM_TEST_EXECUTION_LLM=1 to enable the real-provider proof"
    if os.environ.get("REDTEAM_TEST_EXECUTION_DOCKER") != "1":
        return "set REDTEAM_TEST_EXECUTION_DOCKER=1 under WSL2/Linux with Docker"
    if not os.environ.get("REDTEAM_TEST_EXECUTION_IMAGE"):
        return "set REDTEAM_TEST_EXECUTION_IMAGE to the digest-pinned text-tools image"
    if os.environ.get("REDTEAM_LLM_PROVIDER") != "litellm":
        return "set REDTEAM_LLM_PROVIDER=litellm"
    if not os.environ.get("REDTEAM_LLM_MODEL"):
        return "set REDTEAM_LLM_MODEL to the provider/model identifier"
    key_name = os.environ.get("REDTEAM_LLM_API_KEY_ENV_VAR", "LLM_API_KEY")
    if not os.environ.get(key_name):
        return f"set the API-key environment variable named by {key_name}"
    return None


PROVIDER_SKIP_REASON = _provider_skip_reason()


@pytest.mark.skipif(
    PROVIDER_SKIP_REASON is not None,
    reason=PROVIDER_SKIP_REASON or "real-provider execution proof disabled",
)
async def test_real_provider_selects_fixed_execution_recipe_and_records_evidence(
    tmp_path: Path,
) -> None:
    """Observe a real model selecting the exposed recipe; the bridge fixes its args."""
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    (workspace / "input.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")

    configured = engagement()
    gateway = await gateway_for(
        policy(workspace, image=os.environ["REDTEAM_TEST_EXECUTION_IMAGE"])
    )
    deps = build_test_application(
        control_plane=DefaultControlPlane(
            {configured.engagement_id: configured},
            risk_resolver=RegistryRiskResolver(gateway),
        ),
        tool_gateway=gateway,
        tool_catalog=gateway,
        llm_client=build_llm_client(LLMConfig.from_environment()),
        settings=OrchestrationSettings(max_steps=12, checkpointing=False),
    )
    deps = replace(deps, engagement=configured)

    final = await run_engagement(
        configured,
        deps=deps,
        run_id=RUN_ID,
        objective=(
            "Use the offered fixed command_run option to count the lines in the "
            "approved workspace input. Stop after tool evidence is recorded."
        ),
    )

    assert final.actions_executed >= 1
    evidence = await deps.evidence_store.list_for_run(RUN_ID)
    execution_evidence = [
        item
        for item in evidence
        if item.source_type is EvidenceSourceType.TOOL
        and item.metadata.get("tool_result", {}).get("output", {}).get("stdout", "")
        .lstrip()
        .startswith("3 ")
    ]
    assert execution_evidence
    result = execution_evidence[0].metadata["tool_result"]
    assert result["success"] is True
    assert result["output"]["exit_code"] == 0
