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
    assert loaded.run_policy().probe_limits == ProbeLimits(
        max_actions=16, max_model_requests=64, max_repair_requests=16,
        max_consecutive_provider_failures=6,
    )

    assert loaded.profile.version == "35"
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
    assert "# Verification Worker" in loaded.prompts.compose("native_action", "native contract")
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
    assert loaded.profile.version == "27"
    assert loaded.run_policy().probe_limits == ProbeLimits(
        max_actions=16, max_model_requests=64, max_repair_requests=16,
        max_consecutive_provider_failures=6,
    )
    assert "task_atomization" not in loaded.profile.stages
    assert loaded.run_policy().probe_atomization is False
    assert "probe_atomization" not in loaded.run_policy().model_dump()
    assert "probe_atomization" not in loaded.profile.model_dump()
    planner = loaded.prompts.compose("hypothesis_planning", "legacy contract")
    assert "# Legacy Investigation Planner" in planner
    assert "한 응답에서 수행하는 호환 Planner" in planner
    assert planner.count("당신은") == 1


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
    assert loaded.run_policy().probe_limits == ProbeLimits(
        max_actions=16, max_model_requests=64, max_repair_requests=16,
        max_consecutive_provider_failures=6,
    )


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


def test_plan8_prompt_and_profile_versions_are_consistent():
    expected = {
        "native-probes.yaml": (
            "35",
            {
                "conversation.md": "4",
                "hypothesis-strategy.md": "21",
                "probe-atomizer.md": "13",
                "worker.md": "17",
                "evidence.md": "8",
                "code.md": "4",
            },
        ),
        "native-probes-legacy.yaml": (
            "27",
            {
                "conversation.md": "4",
                "hypothesis-planner.md": "12",
                "worker.md": "17",
                "evidence.md": "8",
                "code.md": "4",
            },
        ),
        "native-specialists.yaml": (
            "9",
            {
                "conversation.md": "4",
                "planner.md": "4",
                "atomizer.md": "5",
                "web.md": "4",
                "mobile.md": "4",
                "evidence.md": "8",
                "code.md": "4",
            },
        ),
        "native-specialists-web.yaml": (
            "7",
            {
                "conversation.md": "4",
                "planner.md": "4",
                "atomizer.md": "5",
                "web.md": "4",
                "evidence.md": "8",
                "code.md": "4",
            },
        ),
    }
    for filename, (profile_version, versions) in expected.items():
        loaded = load_specialist_profile(PROBES.with_name(filename))
        assert loaded.profile.version == profile_version
        assert loaded.profile.common.version == "4"
        assert {
            Path(role.prompt.path).name: role.prompt.version
            for role in loaded.profile.roles
        } == versions


def test_all_active_stages_fit_plan8_static_prompt_limits():
    from redteam.app.cli.conversation import _CONVERSATION_RUNTIME_CONTRACT
    from redteam.orchestration.analysis import _ANALYSIS_SYSTEM, _CLAIM_SYSTEM
    from redteam.orchestration.code_artifacts import GENERATION_SYSTEM, REVIEW_SYSTEM
    from redteam.orchestration.engine import SYSTEM
    from redteam.orchestration.hypothesis_planner import (
        _SINGLE_PLAN_SYSTEM,
        _SPLIT_ATOMIZE_SYSTEM,
        _SPLIT_PLAN_SYSTEM,
    )
    from redteam.orchestration.probes import PROBE_SYSTEM
    from redteam.orchestration.result_claims import RESULT_REVIEW_INSTRUCTION
    from redteam.orchestration.specialist_planner import (
        ATOMIZATION_SYSTEM,
        STRATEGY_SYSTEM,
    )

    profiles = {
        "native-probes.yaml": {
            "hypothesis_planning": _SPLIT_PLAN_SYSTEM,
            "task_atomization": _SPLIT_ATOMIZE_SYSTEM,
            "native_action": SYSTEM + PROBE_SYSTEM,
        },
        "native-probes-legacy.yaml": {
            "hypothesis_planning": _SINGLE_PLAN_SYSTEM,
            "native_action": SYSTEM + PROBE_SYSTEM,
        },
        "native-specialists.yaml": {
            "attack_path_proposal": STRATEGY_SYSTEM,
            "task_atomization": ATOMIZATION_SYSTEM,
            "native_action": SYSTEM,
            "path_review": RESULT_REVIEW_INSTRUCTION,
            "path_draft": STRATEGY_SYSTEM,
            "reference_paths": STRATEGY_SYSTEM,
            "next_action": SYSTEM,
        },
        "native-specialists-web.yaml": {
            "attack_path_proposal": STRATEGY_SYSTEM,
            "task_atomization": ATOMIZATION_SYSTEM,
            "native_action": SYSTEM,
            "path_review": RESULT_REVIEW_INSTRUCTION,
            "path_draft": STRATEGY_SYSTEM,
            "reference_paths": STRATEGY_SYSTEM,
            "next_action": SYSTEM,
        },
    }
    shared = {
        "conversation_decide": _CONVERSATION_RUNTIME_CONTRACT,
        "analysis_verify": _ANALYSIS_SYSTEM,
        "analysis_claim_verify": _CLAIM_SYSTEM,
        "task_completion_review": RESULT_REVIEW_INSTRUCTION,
        "goal_completion_review": RESULT_REVIEW_INSTRUCTION,
        "terminal_criteria_review": RESULT_REVIEW_INSTRUCTION,
        "completion_review": RESULT_REVIEW_INSTRUCTION,
        "code_generation": GENERATION_SYSTEM,
        "code_review": REVIEW_SYSTEM,
    }
    for filename, specific in profiles.items():
        loaded = load_specialist_profile(PROBES.with_name(filename))
        runtime_by_stage = {**shared, **specific}
        assert loaded.profile.stages.keys() <= runtime_by_stage.keys()
        for stage in loaded.profile.stages:
            runtime = runtime_by_stage[stage]
            role_prompt = loaded.prompts.compose(stage, "")
            composed = loaded.prompts.compose(stage, runtime)
            assert len(role_prompt.encode()) < 6 * 1024, (filename, stage)
            assert len(runtime.encode()) < 4 * 1024, (filename, stage)
            assert composed.count("당신은") == 1, (filename, stage)
            if filename == "native-probes.yaml" and stage == "native_action":
                assert len(composed.encode()) < 10 * 1024
        if filename == "native-specialists.yaml":
            mobile = loaded.prompts.compose(
                "native_action", SYSTEM, role_id="mobile-executor",
            )
            assert len(mobile.encode()) < 10 * 1024
            assert mobile.count("당신은") == 1

    prompt_root = PROBES.parent / "prompts"
    assert sum(path.stat().st_size for path in prompt_root.glob("*.md")) < 65 * 1024 / 2


def test_common_roles_and_runtime_contracts_do_not_repeat_three_sentence_blocks():
    import re

    from redteam.app.cli.conversation import _CONVERSATION_RUNTIME_CONTRACT
    from redteam.orchestration.analysis import _ANALYSIS_SYSTEM, _CLAIM_SYSTEM
    from redteam.orchestration.code_artifacts import GENERATION_SYSTEM, REVIEW_SYSTEM
    from redteam.orchestration.engine import SYSTEM
    from redteam.orchestration.hypothesis_planner import (
        _SINGLE_PLAN_SYSTEM,
        _SPLIT_ATOMIZE_SYSTEM,
        _SPLIT_PLAN_SYSTEM,
    )
    from redteam.orchestration.probes import PROBE_SYSTEM
    from redteam.orchestration.specialist_planner import (
        ATOMIZATION_SYSTEM,
        STRATEGY_SYSTEM,
    )

    prompt_root = PROBES.parent / "prompts"
    documents = {"common": (prompt_root / "common.md").read_text()}
    documents.update({
        "role:" + path.name: path.read_text()
        for path in prompt_root.glob("*.md")
        if path.name != "common.md"
    })
    runtime_contracts = (
        _CONVERSATION_RUNTIME_CONTRACT,
        _ANALYSIS_SYSTEM,
        _CLAIM_SYSTEM,
        GENERATION_SYSTEM,
        REVIEW_SYSTEM,
        SYSTEM,
        _SINGLE_PLAN_SYSTEM,
        _SPLIT_PLAN_SYSTEM,
        _SPLIT_ATOMIZE_SYSTEM,
        PROBE_SYSTEM,
        ATOMIZATION_SYSTEM,
        STRATEGY_SYSTEM,
    )
    documents.update({
        f"runtime:{index}": contract
        for index, contract in enumerate(runtime_contracts)
    })

    def windows(text):
        normalized = re.sub(r"[`#*]", "", text)
        normalized = re.sub(r"\s+", " ", normalized)
        sentences = [
            item.strip()
            for item in re.split(r"(?<=[.!?])\s+", normalized)
            if item.strip()
        ]
        return {
            tuple(sentences[index:index + 3])
            for index in range(max(0, len(sentences) - 2))
        }

    blocks = {name: windows(text) for name, text in documents.items()}
    names = sorted(blocks)
    for index, left in enumerate(names):
        for right in names[index + 1:]:
            if left.startswith("runtime:") and right.startswith("runtime:"):
                continue
            assert not blocks[left].intersection(blocks[right]), (left, right)


def test_active_prompt_composition_assigns_one_responsibility_per_stage():
    from redteam.orchestration.analysis import _ANALYSIS_SYSTEM, _CLAIM_SYSTEM
    from redteam.orchestration.code_artifacts import GENERATION_SYSTEM, REVIEW_SYSTEM
    from redteam.orchestration.hypothesis_planner import (
        _SPLIT_ATOMIZE_SYSTEM,
        _SPLIT_PLAN_SYSTEM,
        FEEDBACK_SYSTEM,
    )
    from redteam.orchestration.probes import PROBE_SYSTEM

    loaded = load_specialist_profile(PROBES)
    conversation = loaded.prompts.compose("conversation_decide", "runtime contract")
    strategy = loaded.prompts.compose("hypothesis_planning", "runtime contract")
    atomizer = loaded.prompts.compose("task_atomization", "runtime contract")
    worker_role = loaded.prompts.compose("native_action", "")
    reviewer_role = loaded.prompts.compose("analysis_verify", "")
    author_role = loaded.prompts.compose("code_generation", "")
    worker = loaded.prompts.compose("native_action", PROBE_SYSTEM)
    reviewer = loaded.prompts.compose("analysis_verify", _ANALYSIS_SYSTEM)
    author = loaded.prompts.compose("code_generation", GENERATION_SYSTEM)
    artifact_review = loaded.prompts.compose("code_review", REVIEW_SYSTEM)

    assert "# Conversation Liaison" in conversation
    assert "운영자와 조사 런타임 사이의 대화 담당자" in conversation
    assert conversation.count("당신은") == 1
    assert "# Investigation Planner" in strategy
    assert "다음 보안 질문을 선택하는 Planner" in strategy
    assert strategy.count("당신은") == 1
    assert "candidate_refs=[]" in strategy
    assert "Worker Task와 도구 호출 순서는 만들지 않는다" in strategy
    assert "Finding promotion과 Mission 완료를 구분" in strategy
    assert "# Probe Atomizer" in atomizer
    assert "question을 실행 가능한 Probe Task로 변환" in atomizer
    assert atomizer.count("당신은") == 1
    assert "question을 다시 기획하거나 새 candidate" in atomizer
    assert "criterion을 정확히 한 Task에 배정" in atomizer
    assert "새 Kali-backed 대상 관측" in atomizer
    assert "# Verification Worker" in worker
    assert "하나의 Probe Task를 수행하는 Worker" in worker
    assert worker.count("당신은") == 1
    assert "`graph.record`의 `facts` 입력" in worker
    assert "graph.record.facts" not in worker
    assert "graph.record.branches" not in worker
    assert "`evidence.read`로" in worker
    assert "`task.settle`로 한 번 반환하고 멈춘다" in worker
    assert "# Evidence Reviewer" in reviewer
    assert "독립된 원본 증거 검토자" in reviewer
    assert reviewer.count("당신은") == 1
    assert "`raw_path`/window를 판독" in reviewer
    assert "대상 호출, candidate update, Finding promotion" in reviewer
    assert "evidence.read" not in reviewer
    assert "# Artifact Author" in author
    assert "합성 재현 artifact를 작성하는 코드 작성자" in author
    assert author.count("당신은") == 1
    assert "fixture 성공을 실제 대상 재현이나 Finding으로 주장하지 않는다" in author
    assert "구조화된 정책에 없는 blanket 제한" in strategy
    assert "정책에 없는 읽기 전용·특정 기법 금지" in atomizer
    assert "현재 catalog에 노출된 Kali-backed 외부 실행 도구" in worker
    assert len(conversation.encode()) < 6 * 1024
    assert len(strategy.encode()) < 6 * 1024
    assert len(atomizer.encode()) < 6 * 1024
    assert len(worker_role.encode()) < 6 * 1024
    assert len(reviewer_role.encode()) < 6 * 1024
    assert len(author_role.encode()) < 6 * 1024
    assert len(worker.encode()) < 10 * 1024
    assert len(reviewer.encode()) < 10 * 1024
    assert len(author.encode()) < 10 * 1024
    assert len(artifact_review.encode()) < 10 * 1024
    assert len(FEEDBACK_SYSTEM.encode()) < 4 * 1024
    assert len(_SPLIT_PLAN_SYSTEM.encode()) < 4 * 1024
    assert len(_SPLIT_ATOMIZE_SYSTEM.encode()) < 4 * 1024
    assert len(PROBE_SYSTEM.encode()) < 4 * 1024
    assert len(_CLAIM_SYSTEM.encode()) < 4 * 1024
    assert len(_ANALYSIS_SYSTEM.encode()) < 4 * 1024
    assert len(GENERATION_SYSTEM.encode()) < 4 * 1024
    assert len(REVIEW_SYSTEM.encode()) < 4 * 1024
    assert "physical_cap.budget_available=false" in PROBE_SYSTEM
    assert "pending/running/UNKNOWN invocation은 원래 ID를 보존" in PROBE_SYSTEM
    assert "supported일 때만 adopted_detail" in _CLAIM_SYSTEM
    assert "expectation이나 tool success만으로" in _ANALYSIS_SYSTEM
    assert "source window 밖의 대상 정보" in GENERATION_SYSTEM
    assert "artifact digest를 변경 없이 반환" in REVIEW_SYSTEM


def test_alternate_specialist_profiles_compose_the_same_methodology():
    for filename in ("native-specialists.yaml", "native-specialists-web.yaml"):
        loaded = load_specialist_profile(PROBES.with_name(filename))
        strategy_stage = "attack_path_proposal"
        strategy = loaded.prompts.compose(strategy_stage, "runtime contract")
        atomizer = loaded.prompts.compose("task_atomization", "runtime contract")
        executor = loaded.prompts.compose("native_action", "runtime contract")
        reviewer = loaded.prompts.compose("analysis_verify", "runtime contract")

        assert "# Attack-path Planner" in strategy
        assert "깊이를 coverage보다 우선" in strategy
        assert strategy.count("당신은") == 1
        assert "# Specialist Task Atomizer" in atomizer
        assert "question을 다시 기획하거나 새 subject" in atomizer
        assert atomizer.count("당신은") == 1
        assert "# Web Verification Worker" in executor
        assert "preview에 필요한 내용이 없으면 보존 원본 field를 읽고" in executor
        assert executor.count("당신은") == 1
        assert "독립된 원본 증거 검토자" in reviewer
        assert "대상 호출, candidate update, Finding promotion" in reviewer


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
