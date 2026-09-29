"""Application configuration schema (instruction.md section 19).

So teams don't each invent their own ``.env``/config shape.

``EngagementConfig`` is the file-loadable shape of an ``Engagement`` —
same fields minus ``engagement_id``/``created_at``, which are assigned
at load/run time via ``.to_engagement(...)``. ``AppConfig`` wraps it
alongside per-area config for LLM/Orchestration/Capability/Logging.

Secrets are never stored inline: ``LLMConfig.api_key_env_var`` names an
environment variable, resolved on demand by ``resolve_api_key()`` —
never written into a config file or logged.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from redteam.core.contracts.engagement import (
    ApprovalPolicy,
    Engagement,
    EngagementScope,
    ExecutionBudget,
    RateLimitPolicy,
    RuleOfEngagement,
)
from redteam.core.ids import EngagementId


class EngagementConfig(BaseModel):
    """File-loadable shape for an Engagement.

    TODO(control-team): once real Policy Engine config exists (rule
    files, OPA bundles, ...), reference it from here rather than
    growing this model further.
    """

    name: str
    description: str = ""
    scope: EngagementScope = Field(default_factory=EngagementScope)
    rules_of_engagement: list[RuleOfEngagement] = Field(default_factory=list)
    execution_budget: ExecutionBudget
    rate_limit_policy: RateLimitPolicy = Field(
        default_factory=lambda: RateLimitPolicy(max_actions_per_minute=30)
    )
    approval_policy: ApprovalPolicy = Field(default_factory=ApprovalPolicy)
    metadata: dict[str, Any] = Field(default_factory=dict)

    def to_engagement(
        self, engagement_id: EngagementId, created_at: datetime | None = None
    ) -> Engagement:
        return Engagement(
            engagement_id=engagement_id,
            created_at=created_at or datetime.now(timezone.utc),
            **self.model_dump(),
        )


class LLMConfig(BaseModel):
    provider: str = "fake"  # "fake" | "litellm" — see redteam.llm
    default_model: str = "gpt-4o-mini"
    api_key_env_var: str = "LLM_API_KEY"

    def resolve_api_key(self) -> str | None:
        return os.environ.get(self.api_key_env_var)


class OrchestrationConfig(BaseModel):
    max_retries: int = Field(default=3, ge=0)
    max_steps: int = Field(default=50, ge=1)
    tool_timeout_seconds: float = Field(default=10.0, gt=0, le=600)


class CapabilityConfig(BaseModel):
    mcp_servers: list[str] = Field(default_factory=list)


class LoggingConfig(BaseModel):
    level: str = "INFO"


class AppConfig(BaseModel):
    engagement: EngagementConfig
    llm: LLMConfig = Field(default_factory=LLMConfig)
    orchestration: OrchestrationConfig = Field(default_factory=OrchestrationConfig)
    capability: CapabilityConfig = Field(default_factory=CapabilityConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)


def load_app_config(path: str | Path) -> AppConfig:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return AppConfig.model_validate(raw)


def load_engagement_config(path: str | Path) -> EngagementConfig:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return EngagementConfig.model_validate(raw)
