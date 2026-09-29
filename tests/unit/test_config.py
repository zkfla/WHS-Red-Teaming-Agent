"""Config Schema Test (instruction.md section 19)."""

from __future__ import annotations

import os

from redteam.core.config import (
    AppConfig,
    EngagementConfig,
    LLMConfig,
    load_app_config,
    load_engagement_config,
)
from redteam.core.contracts.engagement import ApprovalPolicy
from redteam.core.enums import RiskLevel
from redteam.core.ids import EngagementId


def test_engagement_config_loads_from_examples_yaml() -> None:
    config = load_engagement_config("examples/engagement.yaml")
    assert config.name
    assert config.execution_budget.max_actions > 0


def test_engagement_config_to_engagement_assigns_runtime_fields() -> None:
    config = EngagementConfig(
        name="t",
        execution_budget={"max_actions": 5},  # type: ignore[arg-type]
    )
    engagement = config.to_engagement(engagement_id=EngagementId("eng_1"))
    assert engagement.engagement_id == EngagementId("eng_1")
    assert engagement.created_at is not None


def test_engagement_config_round_trips_approval_risk_fields() -> None:
    config = EngagementConfig(
        name="t",
        execution_budget={"max_actions": 5},  # type: ignore[arg-type]
        approval_policy=ApprovalPolicy(
            risk_threshold=RiskLevel.LOW,
            auto_approve_up_to=RiskLevel.HIGH,
        ),
    )
    engagement = config.to_engagement(engagement_id=EngagementId("eng_risk_cfg"))
    assert engagement.approval_policy.risk_threshold is RiskLevel.LOW
    assert engagement.approval_policy.auto_approve_up_to is RiskLevel.HIGH


def test_approval_policy_defaults_preserve_legacy_behaviour() -> None:
    policy = ApprovalPolicy()
    assert policy.auto_approve is False
    assert policy.risk_threshold is None
    assert policy.auto_approve_up_to is None


def test_app_config_loads_from_examples_yaml() -> None:
    config = load_app_config("examples/app.yaml")
    assert isinstance(config, AppConfig)
    assert config.engagement.name
    assert config.logging.level


def test_llm_config_resolves_api_key_from_env_var(monkeypatch) -> None:
    monkeypatch.setenv("MY_TEST_LLM_KEY", "sekret")
    config = LLMConfig(api_key_env_var="MY_TEST_LLM_KEY")
    assert config.resolve_api_key() == "sekret"


def test_llm_config_never_stores_the_secret_itself() -> None:
    config = LLMConfig(api_key_env_var="LLM_API_KEY")
    dumped = config.model_dump_json()
    assert os.environ.get("LLM_API_KEY", "__unset__") not in dumped or "__unset__" in dumped
    # the config model itself has no field that could hold a raw secret
    assert "api_key" not in {f for f in LLMConfig.model_fields if f != "api_key_env_var"}
