"""Explicit trusted role/profile assembly; no model/transport calls on load."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_serializer, model_validator

from redteam.core.contracts.specialists import ProbeLimits, SpecialistRunPolicy
from redteam.core.ports.agent_prompts import PromptBinding
from redteam.orchestration.agents.base import AgentSpec
from redteam.orchestration.agents.definition import AgentDefinition
from redteam.orchestration.agents.registry import AgentRegistry
from redteam.orchestration.prompts import PromptSnapshot, RolePrompt


class PromptReference(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    path: str = Field(min_length=1)
    version: str = Field(min_length=1)
    contract_version: str = "1"


class SpecialistRole(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    definition: AgentDefinition
    prompt: PromptReference


class SpecialistProfile(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    schema_version: str = "2"
    profile_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    common: PromptReference
    roles: tuple[SpecialistRole, ...] = Field(min_length=1)
    stages: dict[str, str]
    max_instances: int = Field(ge=1, strict=True)
    max_activations: int = Field(ge=1, strict=True)
    max_tasks_per_strategy: int = Field(ge=1, strict=True)
    max_strategy_revisions: int = Field(ge=1, strict=True)
    probe_limits: ProbeLimits | None = None

    @model_serializer(mode="wrap")
    def _serialize(self, handler):
        result = handler(self)
        if self.probe_limits is None:
            result.pop("probe_limits", None)
        return result

    @model_validator(mode="after")
    def validate_bindings(self):
        identities = [str(role.definition.agent_id) for role in self.roles]
        required = {"native_action",
                    "conversation_decide", "analysis_verify", "analysis_claim_verify",
                    "task_completion_review", "goal_completion_review"}
        if self.schema_version == "2":
            required.update({"attack_path_proposal", "task_atomization"})
            if self.probe_limits is not None:
                raise ValueError("specialist_profile_v2_cannot_enable_probes")
        elif self.schema_version == "3":
            required.add("hypothesis_planning")
            if "task_atomization" in self.stages and self.probe_limits is None:
                raise ValueError("probe_profile_atomizer_requires_probe_limits")
            if self.probe_limits is None:
                raise ValueError("probe_profile_limits_or_stages_invalid")
            if "task_atomization" in self.stages:
                atomizer_id = self.stages["task_atomization"]
                if atomizer_id == self.stages.get("hypothesis_planning"):
                    raise ValueError("probe_profile_atomizer_must_be_separate")
                atomizer = next(
                    (role for role in self.roles
                     if str(role.definition.agent_id) == atomizer_id),
                    None,
                )
                if atomizer is None or atomizer.definition.schedulable:
                    raise ValueError("probe_profile_atomizer_must_be_non_schedulable")
            executors = [role for role in self.roles if role.definition.schedulable]
            if len(executors) != 1 or self.stages.get("native_action") != str(
                executors[0].definition.agent_id
            ):
                raise ValueError("probe_profile_requires_one_common_executor_role")
        if (self.schema_version not in {"2", "3"} or len(set(identities)) != len(identities)
                or required - self.stages.keys() or set(self.stages.values()) - set(identities)):
            raise ValueError("specialist_profile_bindings_invalid")
        if self.schema_version == "2" and (
            self.stages["attack_path_proposal"] == self.stages["task_atomization"]
        ):
            raise ValueError("specialist_planner_atomizer_must_be_separate")
        if not any(role.definition.schedulable for role in self.roles):
            raise ValueError("specialist_executor_missing")
        if any(role.definition.schedulable and not role.definition.capabilities
               for role in self.roles):
            raise ValueError("specialist_executor_capabilities_missing")
        if any(ref.contract_version != "1" for ref in
               [self.common, *(role.prompt for role in self.roles)]):
            raise ValueError("specialist_prompt_contract_unsupported")
        from redteam.orchestration.code_artifacts import validate_stages

        validate_stages(self.stages)
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


def _prompt(root: Path, reference: PromptReference) -> bytes:
    path = Path(reference.path)
    if path.is_absolute() or ".." in path.parts or path.suffix != ".md":
        raise ValueError("specialist_prompt_path_invalid")
    try:
        resolved = (root / path).resolve(strict=True)
    except (OSError, RuntimeError):
        raise ValueError("specialist_file_unavailable") from None
    if not resolved.is_relative_to(root) or not resolved.is_file():
        raise ValueError("specialist_prompt_path_escape")
    return _read_bounded(resolved, 32768)


@dataclass(frozen=True)
class LoadedSpecialists:
    profile: SpecialistProfile
    fingerprint: str
    prompts: PromptSnapshot

    @property
    def code_artifacts_enabled(self) -> bool:
        from redteam.orchestration.code_artifacts import validate_stages

        return validate_stages(self.profile.stages)

    def run_policy(self) -> SpecialistRunPolicy:
        return SpecialistRunPolicy(
            fingerprint=self.fingerprint,
            probe_atomization=(
                self.profile.schema_version == "3"
                and "task_atomization" in self.profile.stages
            ),
            **self.profile.model_dump(include={
                "max_instances", "max_activations", "max_tasks_per_strategy",
                "max_strategy_revisions",
                "probe_limits",
            }),
        )

    def assemble(self, components, *, include_code_artifacts: bool = True):
        from redteam.orchestration.evidence_validity import register_validity

        registry = AgentRegistry()
        for role in self.profile.roles:
            definition = role.definition.model_copy(deep=True)
            registry.register_definition(definition)
            if definition.schedulable:
                registry.register(AgentSpec(
                    agent_id=definition.agent_id, name=definition.name,
                    description=definition.description, capabilities=definition.capabilities,
                ))
        # Keep dev's providers, verifier, mappers, extensions and native engine.
        register_validity(components.local_tools)
        from redteam.orchestration.agents.tool_access import register_role_readiness

        register_role_readiness(components.local_tools)
        if include_code_artifacts and self.code_artifacts_enabled:
            from redteam.orchestration.code_artifacts import register_code_artifacts

            register_code_artifacts(components.local_tools)
        return replace(components, agents=registry)


def load_specialist_profile(path: str | Path) -> LoadedSpecialists:
    try:
        source = Path(path).resolve(strict=True)
    except (OSError, RuntimeError):
        raise ValueError("specialist_file_unavailable") from None
    raw = _read_bounded(source, 65536)
    try:
        profile = SpecialistProfile.model_validate(yaml.safe_load(raw.decode("utf-8")))
    except (ValueError, yaml.YAMLError):
        raise ValueError("specialist_profile_invalid") from None
    common = _prompt(source.parent, profile.common)
    roles: dict[str, RolePrompt] = {}
    for role in profile.roles:
        text = _prompt(source.parent, role.prompt)
        if len(common) + len(text) > 32768:
            raise ValueError("specialist_composed_prompt_oversized")
        identity = str(role.definition.agent_id)
        digest = hashlib.sha256(len(common).to_bytes(8, "big") + common + text).hexdigest()
        roles[identity] = RolePrompt(PromptBinding(
            role_id=identity, version=role.prompt.version,
            contract_version=role.prompt.contract_version, sha256=digest,
        ), text.decode("utf-8"))
    binding = {"profile": profile.model_dump(mode="json"),
               "prompts": {key: value.binding.model_dump() for key, value in roles.items()}}
    fingerprint = hashlib.sha256(json.dumps(binding, sort_keys=True).encode()).hexdigest()
    return LoadedSpecialists(profile, fingerprint, PromptSnapshot(
        common.decode("utf-8"), roles, profile.stages,
    ))


def selected_specialists(settings) -> LoadedSpecialists | None:
    if settings.specialist_profile_path is None:
        return None
    if settings.llm_request_policy is None:
        raise ValueError("specialist_mode_requires_explicit_llm_request_policy")
    return load_specialist_profile(settings.specialist_profile_path)
