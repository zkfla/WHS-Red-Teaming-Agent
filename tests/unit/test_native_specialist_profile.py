"""Trusted selection, confined prompt files and explicit request policy."""

from pathlib import Path

import pytest
import yaml

from redteam.app.agent_profile import load_specialist_profile, selected_specialists
from redteam.core.config import OrchestrationConfig
from redteam.orchestration.settings import OrchestrationSettings

PROFILE = Path(__file__).parents[2] / "config/agents/native-specialists.yaml"


def test_web_only_profile_reuses_prompts_without_mobile_execution_binding():
    full = load_specialist_profile(PROFILE)
    web = load_specialist_profile(PROFILE.with_name("native-specialists-web.yaml"))
    assert web.fingerprint != full.fingerprint
    assert web.code_artifacts_enabled
    assert len(web.profile.roles) == 6
    assert [str(role.definition.agent_id) for role in web.profile.roles
            if role.definition.schedulable] == ["web-executor"]
    for role in web.profile.roles:
        identity = str(role.definition.agent_id)
        assert web.prompts.compose("native_action", "contract", role_id=identity) == (
            full.prompts.compose("native_action", "contract", role_id=identity)
        )
    with pytest.raises(ValueError, match="binding_missing"):
        web.prompts.compose("native_action", "contract", role_id="mobile-executor")


def test_environment_policy_reaches_cli_settings_without_implicit_unlimited():
    config = OrchestrationConfig.from_environment({
        "REDTEAM_SPECIALIST_PROFILE": str(PROFILE),
        "REDTEAM_LLM_REQUEST_POLICY_JSON": '{"mode":"unlimited_requests","max_requests":null}',
    })
    settings = OrchestrationSettings.from_config(config)
    assert settings.specialist_profile_path == str(PROFILE)
    assert settings.llm_request_policy.mode == "unlimited_requests"
    assert selected_specialists(settings).profile.profile_id == "native-specialists"
    for raw in ('{}', '{"mode":"bounded"}', '{"mode":"bounded","max_requests":0}'):
        with pytest.raises(ValueError):
            OrchestrationConfig.from_environment({"REDTEAM_LLM_REQUEST_POLICY_JSON": raw})
    with pytest.raises(ValueError, match="explicit_llm_request_policy"):
        selected_specialists(OrchestrationSettings(specialist_profile_path=str(PROFILE)))


def test_native_contract_and_stage_prompts_not_json_path_contract():
    loaded = load_specialist_profile(PROFILE)
    native = loaded.prompts.compose("native_action", "NATIVE TOOLS", role_id="mobile-executor")
    assert "Mobile execution worker" in native
    assert "NATIVE TOOLS" in native
    assert "response_schema" not in native
    assert "Web execution worker" not in native
    assert "Investigation planner" in loaded.prompts.compose("attack_path_proposal", "schema")
    with pytest.raises(ValueError, match="binding_missing"):
        loaded.prompts.compose("unknown-stage", "schema")


def test_prompt_changes_update_binding_and_symlink_escape_rejected(tmp_path):
    import shutil

    shutil.copytree(PROFILE.parent, tmp_path / "agents")
    path = tmp_path / "agents/native-specialists.yaml"
    original = load_specialist_profile(path)
    snapshot = original.prompts.compose("native_action", "native tools", role_id="web-executor")
    prompt = path.parent / "prompts/web.md"
    prompt.write_text("# changed trusted instructions", encoding="utf-8")
    assert load_specialist_profile(path).fingerprint != original.fingerprint
    assert original.prompts.compose("native_action", "native tools",
                                    role_id="web-executor") == snapshot
    prompt.unlink()
    prompt.symlink_to(PROFILE.parent / "prompts/web.md")
    with pytest.raises(ValueError, match="path_escape"):
        load_specialist_profile(path)


def test_arbitrary_registered_role_ids_not_closed_seven_name_switch(tmp_path):
    import shutil

    shutil.copytree(PROFILE.parent, tmp_path / "agents")
    path = tmp_path / "agents/native-specialists.yaml"
    raw = yaml.safe_load(path.read_text())
    for role in raw["roles"]:
        role["definition"]["agent_id"] = "custom-" + role["definition"]["agent_id"]
    raw["stages"] = {stage: "custom-" + identity for stage, identity in raw["stages"].items()}
    path.write_text(yaml.safe_dump(raw))
    loaded = load_specialist_profile(path)
    assert loaded.prompts.binding("task_atomization").role_id == "custom-atomizer"
    assert loaded.prompts.binding("code_generation").role_id == "custom-code-author"
    assert loaded.code_artifacts_enabled


@pytest.mark.parametrize("mode", ["absent", "partial", "same-role"])
def test_optional_code_feature_requires_two_independent_explicit_bindings(tmp_path, mode):
    import shutil

    shutil.copytree(PROFILE.parent, tmp_path / "agents")
    path = tmp_path / "agents/native-specialists.yaml"
    raw = yaml.safe_load(path.read_text())
    if mode in {"absent", "partial"}:
        del raw["stages"]["code_review"]
        if mode == "absent":
            del raw["stages"]["code_generation"]
    else:
        raw["stages"]["code_review"] = raw["stages"]["code_generation"]
    path.write_text(yaml.safe_dump(raw))
    if mode == "absent":
        assert not load_specialist_profile(path).code_artifacts_enabled
    else:
        with pytest.raises(ValueError, match="specialist_profile_invalid"):
            load_specialist_profile(path)


async def test_changed_prompt_binding_cannot_resume_same_run(tmp_path):
    from dataclasses import replace

    from redteam.orchestration.component_binding import builtin_component_binding
    from redteam.orchestration.runner import AgentLoopRunner
    from redteam.orchestration.state import RuntimeState
    from tests.integration.test_native_react_loop import (
        ScriptedNativeLLM,
        _engagement,
        _native_deps,
    )

    original = load_specialist_profile(PROFILE)
    engagement = _engagement()
    binding = builtin_component_binding().model_copy(update={
        "specialist_fingerprint": original.fingerprint,
    })
    deps = replace(_native_deps(engagement, ScriptedNativeLLM([])), component_binding=binding)
    runtime = RuntimeState(run_id="run-binding", engagement_id=engagement.engagement_id,
                           component_binding=binding)
    async with AgentLoopRunner(deps) as runner:
        assert await runner._binding_for(runtime)
    changed = replace(deps, component_binding=binding.model_copy(update={
        "specialist_fingerprint": "f" * 64,
    }))
    async with AgentLoopRunner(changed) as runner:
        assert not await runner._binding_for(runtime)
