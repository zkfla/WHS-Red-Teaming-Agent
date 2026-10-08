"""Operator-selected serial specialist profile, validated before transports open."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from redteam.core.ports.agent_prompts import PromptBinding
from redteam.core.ports.fixture_runner import FixturePolicy
from redteam.orchestration.agents.base import AgentSpec
from redteam.orchestration.agents.definition import AgentDefinition
from redteam.orchestration.agents.packages import RolePackage, register_role_package
from redteam.orchestration.agents.providers import CandidateProviderRegistry
from redteam.orchestration.agents.registry import AgentRegistry
from redteam.orchestration.local_tools import LocalToolRegistry
from redteam.orchestration.prompts import PromptSnapshot, RolePrompt
from redteam.orchestration.settings import OrchestrationSettings
from redteam.orchestration.specialist_planning import SerialSpecialistPlanningProvider


class PromptReference(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    path: str = Field(min_length=1)
    version: str = Field(min_length=1)
    contract_version: str = Field(min_length=1)


class SpecialistRole(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    definition: AgentDefinition
    prompt: PromptReference


class SpecialistProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    profile_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    schema_version: str = "1"
    prompt_root: str = Field(min_length=1)
    common: PromptReference
    roles: tuple[SpecialistRole, ...] = Field(min_length=1)
    stages: dict[str, str]
    max_tasks_per_question: int = Field(default=6, ge=1, le=6)
    workflow: Literal["serial", "durable"] = "serial"
    worker_batch_size: int | None = Field(default=None, ge=1, le=2)
    decision_concurrency: int = Field(default=1, ge=1, le=2)
    decision_pool_limit: int | None = Field(default=None, ge=1)
    evidence_validity: bool = False
    validity_wait_seconds: float = Field(default=0, ge=0, le=60)
    code_artifacts: bool = False
    fixture_policy: FixturePolicy | None = None

    @model_validator(mode="after")
    def check_bindings(self):
        if self.validity_wait_seconds and (
            not self.evidence_validity or self.workflow != "durable"
        ):
            raise ValueError("specialist_validity_wait_requires_durable_validity")
        if self.schema_version != "1" or not self.version.strip():
            raise ValueError("specialist_profile_version_unsupported")
        if self.worker_batch_size is not None and self.workflow != "durable":
            raise ValueError("specialist_parallel_execution_requires_durable_workflow")
        if self.decision_concurrency > 1 and (
            self.workflow != "durable" or self.decision_pool_limit is None
        ):
            raise ValueError("specialist_parallel_reasoning_requires_durable_finite_pool")
        identities = [str(role.definition.agent_id) for role in self.roles]
        if len(set(identities)) != len(identities):
            raise ValueError("specialist_role_duplicate")
        if set(self.stages.values()) - set(identities):
            raise ValueError("specialist_stage_role_missing")
        required = {
            "attack_path_proposal",
            "task_atomization",
            "path_review",
            "next_action",
            "completion_review",
            "conversation_decide",
            "analysis_verify",
            "analysis_claim_verify",
            "task_completion_review",
            "goal_completion_review",
        }
        if required - set(self.stages):
            raise ValueError("specialist_stage_binding_missing")
        optional = {"code_generation", "code_review"} if self.code_artifacts else set()
        if self.code_artifacts and (
            optional - set(self.stages) or self.fixture_policy is None
            or self.stages.get("code_generation") == self.stages.get("code_review")
        ):
            raise ValueError("specialist_code_review_or_fixture_policy_missing")
        if not self.code_artifacts and self.fixture_policy is not None:
            raise ValueError("specialist_fixture_policy_without_artifacts")
        if set(self.stages) - required - optional:
            raise ValueError("specialist_stage_unsupported")
        for reference in [self.common, *(role.prompt for role in self.roles)]:
            if not reference.version.strip() or reference.contract_version != "1":
                raise ValueError("specialist_prompt_version_unsupported")
        if self.stages["attack_path_proposal"] == self.stages["task_atomization"]:
            raise ValueError("specialist_planner_atomizer_must_be_separate")
        if any(
            role.definition.schedulable and not role.definition.capabilities for role in self.roles
        ):
            raise ValueError("specialist_executor_capabilities_missing")
        return self


def _read_bounded(path: Path, limit: int) -> bytes:
    try:
        with path.open("rb") as source:
            content = source.read(limit + 1)
    except (OSError, RuntimeError):
        raise ValueError("specialist_file_unavailable") from None
    if not content.strip() or len(content) > limit:
        raise ValueError("specialist_file_empty_or_oversized")
    return content


def _confined(root: Path, reference: str) -> Path:
    relative = Path(reference)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("specialist_path_escape")
    try:
        resolved = (root / relative).resolve(strict=True)
    except (OSError, RuntimeError):
        raise ValueError("specialist_file_unavailable") from None
    if not resolved.is_relative_to(root) or not resolved.is_file():
        raise ValueError("specialist_path_escape")
    return resolved


@dataclass(frozen=True)
class LoadedSpecialists:
    path: str
    fingerprint: str
    profile: SpecialistProfile
    prompts: PromptSnapshot
    bindings: dict[str, dict[str, Any]]

    def configure_execution(self, settings: OrchestrationSettings) -> OrchestrationSettings:
        """Explicit profile batch selection; preserve all Control/store/target limits.

        The profile owns batch size when declared. Global concurrency remains
        an upper bound; P0/undeclared profiles do not change runtime settings.
        """
        batch = self.profile.worker_batch_size
        values = settings.model_dump()
        if batch is not None:
            values["max_actions_per_turn"] = min(batch, settings.global_concurrency)
        values["specialist_decision_concurrency"] = self.profile.decision_concurrency
        values["specialist_decision_pool_limit"] = self.profile.decision_pool_limit
        return OrchestrationSettings.model_validate(values)

    def assemble(self, components):
        agents = AgentRegistry()
        providers = CandidateProviderRegistry()
        for entry in self.profile.roles:
            definition = entry.definition.model_copy(deep=True)
            spec = (
                AgentSpec(
                    agent_id=definition.agent_id,
                    name=definition.name,
                    description=definition.description,
                    capabilities=definition.capabilities,
                )
                if definition.schedulable
                else None
            )
            register_role_package(
                agents,
                providers,
                RolePackage(
                    agent_id=definition.agent_id,
                    definition=definition,
                    spec=spec,
                    provider_id=definition.provider_id,
                ),
            )
        local_tools = (components.local_tools.copy() if components.local_tools is not None
                       else LocalToolRegistry())
        if self.profile.evidence_validity:
            from redteam.orchestration.evidence_validity import register_validity

            register_validity(local_tools)
        if self.profile.code_artifacts:
            from redteam.orchestration.code_artifacts import register_code_artifacts

            register_code_artifacts(local_tools)
        waiter = components.condition_waiter
        if self.profile.validity_wait_seconds:
            from redteam.orchestration.evidence_expiry import EvidenceExpiryWaiter

            waiter = EvidenceExpiryWaiter(self.profile.validity_wait_seconds)
        return replace(
            components,
            agents=agents,
            candidate_providers=providers,
            local_tools=local_tools,
            condition_waiter=waiter,
            planning_provider=SerialSpecialistPlanningProvider(
                self.prompts,
                max_tasks_per_question=self.profile.max_tasks_per_question,
                enable_replanning=self.profile.workflow == "durable",
            ),
        )


def load_specialist_profile(path: str | Path) -> LoadedSpecialists:
    """Resolve relative prompt refs against the explicit profile, never a user's home."""
    try:
        source = Path(path).resolve(strict=True)
    except (OSError, RuntimeError):
        raise ValueError("specialist_file_unavailable") from None
    content = _read_bounded(source, 65536)
    try:
        profile = SpecialistProfile.model_validate(yaml.safe_load(content.decode("utf-8")))
    except (ValueError, yaml.YAMLError):
        raise ValueError("specialist_profile_invalid") from None
    base = source.parent
    relative_root = Path(profile.prompt_root)
    if relative_root.is_absolute() or ".." in relative_root.parts:
        raise ValueError("specialist_path_escape")
    try:
        root = (base / relative_root).resolve(strict=True)
    except (OSError, RuntimeError):
        raise ValueError("specialist_file_unavailable") from None
    if not root.is_relative_to(base) or not root.is_dir():
        raise ValueError("specialist_path_escape")

    def read(reference: PromptReference) -> bytes:
        if Path(reference.path).suffix != ".md":
            raise ValueError("specialist_prompt_not_markdown")
        return _read_bounded(_confined(root, reference.path), 32768)

    common = read(profile.common)
    bindings: dict[str, dict[str, Any]] = {}
    roles: dict[str, RolePrompt] = {}
    for entry in profile.roles:
        raw = read(entry.prompt)
        if len(common) + len(raw) > 32768:
            raise ValueError("specialist_composed_prompt_oversized")
        identity = str(entry.definition.agent_id)
        digest = hashlib.sha256(len(common).to_bytes(8, "big") + common + raw).hexdigest()
        binding = PromptBinding(
            role_id=identity,
            version=entry.prompt.version,
            contract_version=entry.prompt.contract_version,
            sha256=digest,
        )
        try:
            text = raw.decode("utf-8")
        except UnicodeError:
            raise ValueError("specialist_prompt_invalid_encoding") from None
        roles[identity] = RolePrompt(binding=binding, text=text)
        bindings[identity] = binding.model_dump(mode="json")
    fingerprint = hashlib.sha256(
        content + json.dumps(bindings, sort_keys=True).encode("utf-8")
    ).hexdigest()
    try:
        common_text = common.decode("utf-8")
    except UnicodeError:
        raise ValueError("specialist_prompt_invalid_encoding") from None
    return LoadedSpecialists(
        path=str(source),
        fingerprint=fingerprint,
        profile=profile,
        prompts=PromptSnapshot(common_text, roles, profile.stages),
        bindings=bindings,
    )
