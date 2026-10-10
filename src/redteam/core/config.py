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

import json
import os
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from redteam.core.contracts.engagement import (
    ApprovalPolicy,
    Engagement,
    EngagementScope,
    ExecutionBudget,
    ExternalTextPolicy,
    RateLimitPolicy,
    RuleOfEngagement,
    TargetActionPolicy,
)
from redteam.core.contracts.llm_policy import LLMRequestPolicy
from redteam.core.environment import (
    extensions_env_raw as _extensions_env_raw,
)
from redteam.core.environment import (
    llm_env_values as _llm_env_values,
)
from redteam.core.environment import (
    methodology_env_raw as _methodology_env_raw,
)
from redteam.core.environment import (
    orchestration_env_values as _orchestration_env_values,
)
from redteam.core.environment import (
    planning_provider_env_raw as _planning_provider_env_raw,
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
    target_action_policies: list[TargetActionPolicy] = Field(default_factory=list)
    external_text_policy: ExternalTextPolicy = Field(default_factory=ExternalTextPolicy)
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
    provider: str = "litellm"  # Production requires the real LLM adapter.
    default_model: str = "gpt-4o-mini"
    api_key_env_var: str = "LLM_API_KEY"

    def resolve_api_key(self) -> str | None:
        return os.environ.get(self.api_key_env_var)

    @classmethod
    def from_environment(cls, env: Mapping[str, str] | None = None) -> LLMConfig:
        """The CLI's only source for ``llm`` (mirrors
        ``MCPServerSettings.from_environment``): ``REDTEAM_LLM_PROVIDER``/
        ``REDTEAM_LLM_MODEL``/``REDTEAM_LLM_API_KEY_ENV_VAR``.

        ``REDTEAM_LLM_PROVIDER`` is required here -- there is no implicit
        default. A production run with no provider set is a real
        misconfiguration and must fail loudly, not silently run
        against ``FakeLLMClient`` (which the old "default to the
        ``provider='fake'`` field value" behavior amounted to). Using the
        test clients must be explicitly injected by the test composition root.
        An unrecognized provider string (a typo, or a name only a *real*
        provider would use, like ``"openai"``) is the same error, not a
        silent fallback either; ``build_llm_client`` enforces the actual
        provider allowlist, this only enforces that *something* was said.
        Raw values come from the low common input boundary so the CLI
        and the library share one collection path. Pass an explicit
        mapping to validate that snapshot instead of the live process env.
        """
        vals = _llm_env_values(source=env)
        provider = vals.get("provider")
        if not provider:
            raise ValueError(
                "REDTEAM_LLM_PROVIDER is not set. Set it explicitly -- "
                "REDTEAM_LLM_PROVIDER=litellm (with REDTEAM_LLM_MODEL) for "
                "a real provider. There is no implicit default."
            )
        kwargs: dict[str, str] = {"provider": provider}
        model = vals.get("model")
        if model:
            kwargs["default_model"] = model
        api_key_env_var = vals.get("api_key_env_var")
        if api_key_env_var:
            kwargs["api_key_env_var"] = api_key_env_var
        return cls(**kwargs)


class OrchestrationConfig(BaseModel):
    """실행 상한 전용 설정. 후보 생성 방식 선택지가 아니다.

    계획 수립은 항상 언어모델 주도이며, 규칙 후보는 참고용이다.
    구 이름만 호환용으로 남겼고 값은 항상 참으로 고정된다.
    거짓은 규칙 경로 선택이 아니라 검증 오류로 거부된다.
    """

    max_retries: int = Field(default=3, ge=0)
    max_steps: int = Field(default=50, ge=1)
    llm_request_policy: LLMRequestPolicy | None = None
    specialist_profile_path: str | None = Field(default=None, min_length=1, max_length=4096)
    tool_timeout_seconds: float = Field(default=10.0, gt=0, le=600)
    resource_wait_timeout_seconds: float = Field(default=30.0, gt=0, le=600)
    #: Legacy opt-in name retained for compatibility; not a mode switch.
    #: Planning is always LLM-led — False (or "false") fails validation
    #: instead of selecting a rule path.
    llm_candidate_generation: Literal[True] = True

    @classmethod
    def from_environment(cls, env: Mapping[str, str] | None = None) -> OrchestrationConfig:
        """CLI runtime settings, validated with the same rules as library config.

        Raw values come from the low common input boundary. Pass an
        explicit mapping to validate that snapshot instead of live env.
        """
        values: dict[str, Any] = dict(_orchestration_env_values(source=env))
        if "llm_request_policy" in values:
            values["llm_request_policy"] = LLMRequestPolicy.model_validate_json(
                values["llm_request_policy"]
            )
        if "llm_candidate_generation" in values:
            values["llm_candidate_generation"] = TypeAdapter(bool).validate_python(
                str(values["llm_candidate_generation"]).strip()
            )
        return cls.model_validate(values)


class MethodologyConfig(BaseModel):
    """Explicit operator-owned reference deployment, never Agent input."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    workspace_policy_path: str = Field(min_length=1, max_length=4096)
    bundle_policy_path: str = Field(min_length=1, max_length=4096)
    prefix: str = Field(min_length=1, max_length=256)
    reference_ids: tuple[str, ...] = Field(min_length=1, max_length=3)
    read_chars: int = Field(default=8192, ge=256, le=16384)
    max_context_chars: int = Field(default=12000, ge=256, le=24000)

    @classmethod
    def from_environment(cls, env: Mapping[str, str] | None = None) -> MethodologyConfig | None:
        value = _methodology_env_raw(source=env)
        if value is None:
            return None
        if not value.strip() or len(value) > 16384:
            raise ValueError("methodology_config_invalid")
        try:
            return cls.model_validate_json(value)
        except ValueError:
            raise ValueError("methodology_config_invalid") from None


class CapabilityConfig(BaseModel):
    mcp_servers: list[str] = Field(default_factory=list)
    methodology: MethodologyConfig | None = None
    role_manifest_path: str | None = None


def extensions_from_environment(env: Mapping[str, str] | None = None) -> list[str]:
    raw = _extensions_env_raw(source=env)
    if raw is None or not raw.strip():
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("REDTEAM_EXTENSIONS must be a JSON list") from exc
    if not isinstance(parsed, list):
        raise ValueError("REDTEAM_EXTENSIONS must be a JSON list")
    return TypeAdapter(list[str]).validate_python(parsed)


def planning_provider_from_environment(env: Mapping[str, str] | None = None) -> str | None:
    raw = _planning_provider_env_raw(source=env)
    if raw is None or not raw.strip():
        return None
    return raw.strip()


class LoggingConfig(BaseModel):
    level: str = "INFO"


class AppConfig(BaseModel):
    engagement: EngagementConfig
    llm: LLMConfig = Field(default_factory=LLMConfig)
    orchestration: OrchestrationConfig = Field(default_factory=OrchestrationConfig)
    capability: CapabilityConfig = Field(default_factory=CapabilityConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    extensions: list[str] = Field(default_factory=list)
    planning_provider: str | None = None


def load_app_config(path: str | Path) -> AppConfig:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return AppConfig.model_validate(raw)


def load_engagement_config(path: str | Path) -> EngagementConfig:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return EngagementConfig.model_validate(raw)
