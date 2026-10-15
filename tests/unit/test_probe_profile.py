"""Trusted generic profile/schema tests without models or transport calls."""

import hashlib
import json
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from redteam.app.agent_profile import (
    SpecialistProfile,
    load_specialist_profile,
    selected_specialists,
)
from redteam.core.config import DEFAULT_SPECIALIST_PROFILE_PATH, OrchestrationConfig
from redteam.core.contracts.llm_policy import LLMRequestPolicy
from redteam.core.contracts.specialists import ProbeLimits, SpecialistRunPolicy
from redteam.orchestration.agents.role_view import tools_for_role
from redteam.orchestration.metered_llm import current_request_scope, llm_request_scope
from redteam.orchestration.settings import OrchestrationSettings
from tests.integration.test_central_development import PROFILE, gateway

PROBES = PROFILE.with_name("native-probes.yaml")
LEGACY = PROFILE.with_name("native-probes-legacy.yaml")


def test_probe_profile_uses_one_common_executor_and_split_atomizer():
    loaded = load_specialist_profile(PROBES)
    executors = [role for role in loaded.profile.roles if role.definition.schedulable]
    assert len(executors) == 1
    assert loaded.run_policy().probe_limits == ProbeLimits(max_actions=4, max_model_requests=16)
    assert loaded.profile.version == "11"
    atomizer_id = loaded.profile.stages.get("task_atomization")
    assert atomizer_id == "probe-atomizer"
    assert atomizer_id != loaded.profile.stages["hypothesis_planning"]
    atomizer = next(
        role for role in loaded.profile.roles
        if str(role.definition.agent_id) == atomizer_id
    )
    assert not atomizer.definition.schedulable
    assert loaded.run_policy().probe_atomization is True
    assert loaded.run_policy().model_dump()["probe_atomization"] is True
    assert "Common probe worker" in loaded.prompts.compose("native_action", "native contract")
    assert loaded.prompts.compose("hypothesis_planning", "native contract")
    assert loaded.prompts.compose("task_atomization", "native contract")
    assert loaded.code_artifacts_enabled
    raw = loaded.profile.model_dump()
    idx = next(
        i for i, role in enumerate(raw["roles"])
        if role["definition"]["agent_id"] == raw["stages"]["native_action"]
    )
    old = raw["roles"][idx]["definition"]["agent_id"]
    raw["roles"][idx]["definition"]["agent_id"] = "another-instance-family"
    raw["stages"]["native_action"] = "another-instance-family"
    assert old != SpecialistProfile.model_validate(raw).stages["native_action"]


def test_legacy_single_stage_profile_has_no_atomizer():
    loaded = load_specialist_profile(LEGACY)
    assert loaded.profile.version == "6"
    assert "task_atomization" not in loaded.profile.stages
    assert loaded.run_policy().probe_atomization is False
    assert "probe_atomization" not in loaded.run_policy().model_dump()
    assert "probe_atomization" not in loaded.profile.model_dump()


def test_new_cli_and_application_config_default_to_candidate_profile():
    config = OrchestrationConfig.from_environment({})
    assert config.specialist_profile_path == DEFAULT_SPECIALIST_PROFILE_PATH
    settings = OrchestrationSettings.from_config(config).model_copy(update={
        "llm_request_policy": LLMRequestPolicy(mode="bounded", max_requests=10),
    })
    loaded = selected_specialists(settings)
    assert loaded is not None
    assert loaded.profile.profile_id == "native-probes"
    assert loaded.profile.schema_version == "3"
    assert loaded.profile.stages["task_atomization"] == "probe-atomizer"
    assert loaded.run_policy().probe_atomization is True


def test_legacy_profile_fingerprint_is_not_changed_by_optional_probe_fields():
    source = Path(PROFILE)
    loaded = load_specialist_profile(source)
    raw = yaml.safe_load(source.read_text())
    assert "probe_limits" not in loaded.profile.model_dump()
    assert "probe_limits" not in loaded.run_policy().model_dump()
    assert "probe_atomization" not in loaded.run_policy().model_dump()
    roles = {}
    common = (source.parent / raw["common"]["path"]).read_bytes()
    for role in raw["roles"]:
        reference = role["prompt"]
        text = (source.parent / reference["path"]).read_bytes()
        digest = hashlib.sha256(len(common).to_bytes(8, "big") + common + text).hexdigest()
        roles[role["definition"]["agent_id"]] = {
            "role_id": role["definition"]["agent_id"], "version": reference["version"],
            "contract_version": "1", "sha256": digest,
        }
    # Model normalization already existed before the additive probe field.
    binding = {"profile": loaded.profile.model_dump(mode="json"), "prompts": roles}
    expected = hashlib.sha256(json.dumps(binding, sort_keys=True).encode()).hexdigest()
    assert loaded.fingerprint == expected


def test_split_policy_serialization_keeps_legacy_fingerprints_stable():
    default = load_specialist_profile(PROBES)
    legacy = load_specialist_profile(LEGACY)
    assert default.run_policy().model_dump()["probe_atomization"] is True
    assert "probe_atomization" not in legacy.run_policy().model_dump()
    assert "probe_atomization" not in legacy.profile.model_dump()


def _split_raw():
    return yaml.safe_load(PROBES.read_text())


def test_v3_atomizer_must_bind_a_distinct_role():
    raw = _split_raw()
    raw["stages"]["task_atomization"] = raw["stages"]["hypothesis_planning"]
    with pytest.raises(ValidationError):
        SpecialistProfile.model_validate(raw)


def test_v3_atomizer_must_be_non_schedulable():
    raw = _split_raw()
    raw["stages"]["task_atomization"] = raw["stages"]["native_action"]
    with pytest.raises(ValidationError):
        SpecialistProfile.model_validate(raw)


def test_v3_atomizer_without_probe_limits_is_rejected():
    raw = _split_raw()
    del raw["probe_limits"]
    with pytest.raises(ValidationError):
        SpecialistProfile.model_validate(raw)


def test_v2_never_sets_probe_atomization():
    loaded = load_specialist_profile(PROFILE)
    assert loaded.profile.schema_version == "2"
    assert loaded.run_policy().probe_atomization is False
    assert "probe_atomization" not in loaded.run_policy().model_dump()


def test_run_policy_derives_flag_from_public_stages_only():
    default = load_specialist_profile(PROBES)
    assert default.run_policy().probe_atomization == (
        default.profile.schema_version == "3"
        and "task_atomization" in default.profile.stages
    )
    legacy = load_specialist_profile(LEGACY)
    assert legacy.run_policy().probe_atomization == (
        legacy.profile.schema_version == "3"
        and "task_atomization" in legacy.profile.stages
    )


def test_existing_selection_contract_allows_multiple_providers_and_excludes_unselected():
    loaded = load_specialist_profile(PROBES)
    definition = next(role.definition for role in loaded.profile.roles
                      if role.definition.schedulable)
    descriptor = gateway().catalog.all_descriptors()[0]
    mobile = descriptor.model_copy(update={"tool_id": "mobile.custom",
                                           "server_id": "other-provider",
                                           "capability_tags": ["mobile"]})
    cloud = descriptor.model_copy(update={"tool_id": "cloud.custom", "capability_tags": ["cloud"]})
    from redteam.capability.development import DevelopmentToolGateway

    catalog = DevelopmentToolGateway([descriptor, mobile, cloud], []).catalog
    assert {str(item.tool_id) for item in tools_for_role(definition, catalog)} == {
        str(descriptor.tool_id), "mobile.custom",
    }


def test_request_scope_keeps_task_identity_through_reviews_and_isolates_other_runs():
    with llm_request_scope("run-a", "worker-a", task_id="task-a"):
        with llm_request_scope("run-a", "reviewer-a"):
            assert current_request_scope().task_id == "task-a"
        with llm_request_scope("run-b", "worker-b"):
            assert current_request_scope().task_id is None
        assert current_request_scope().owner == "worker-a"
    assert current_request_scope() is None


@pytest.mark.parametrize("value", [0, True, 1.5])
def test_probe_limits_are_finite_validated_integers(value):
    with pytest.raises(ValidationError):
        ProbeLimits(max_actions=value)


def test_policy_rejects_atomization_without_probe_limits():
    fingerprint = "0" * 64
    with pytest.raises(ValidationError):
        SpecialistRunPolicy(
            fingerprint=fingerprint,
            max_instances=2,
            max_activations=8,
            max_tasks_per_strategy=8,
            max_strategy_revisions=4,
            probe_atomization=True,
        )
    policy = SpecialistRunPolicy(
        fingerprint=fingerprint,
        max_instances=2,
        max_activations=8,
        max_tasks_per_strategy=8,
        max_strategy_revisions=4,
        probe_limits=ProbeLimits(),
        probe_atomization=True,
    )
    assert policy.model_dump()["probe_atomization"] is True
    legacy = SpecialistRunPolicy(
        fingerprint=fingerprint,
        max_instances=2,
        max_activations=8,
        max_tasks_per_strategy=8,
        max_strategy_revisions=4,
    )
    assert "probe_atomization" not in legacy.model_dump()
