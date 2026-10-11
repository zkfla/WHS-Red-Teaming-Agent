"""Optional offline mechanism selected only through public application assembly."""

import json

import pytest

from redteam.app.bootstrap import build_application
from redteam.capability.code_fixtures import WorkroomCodeFixtureRunner
from redteam.core.config import AppConfig, EngagementConfig, OrchestrationConfig
from redteam.core.contracts.llm_policy import LLMRequestPolicy
from redteam.orchestration.settings import OrchestrationSettings
from redteam.orchestration.specialist_planner import specialist_planning_inputs
from tests.fakes import DummyToolGateway, FakeControlPlane, FakeEvidenceStore
from tests.integration.test_code_artifacts import AuthorReviewer
from tests.integration.test_code_fixture_validation import Runner
from tests.integration.test_native_react_loop import _engagement
from tests.unit.test_native_specialist_profile import PROFILE


async def assemble(tmp_path, *, policy=None, selected=True, injected=None, gateway=None):
    engagement = _engagement()
    config = AppConfig(engagement=EngagementConfig.model_validate(
        engagement.model_dump(exclude={"engagement_id", "created_at"}),
    ), orchestration=OrchestrationConfig(
        specialist_profile_path=str(PROFILE) if selected else None,
        llm_request_policy=LLMRequestPolicy(mode="bounded", max_requests=10),
        code_fixture_policy_path=str(policy) if policy else None,
    ))
    gateway = gateway or DummyToolGateway(tools=[])
    return await build_application(
        config, llm=AuthorReviewer(), control_plane=FakeControlPlane(),
        tool_gateway=gateway, tool_catalog=gateway, evidence_store=FakeEvidenceStore(),
        evidence_dir=tmp_path / "evidence", audit_dir=tmp_path / "audit",
        approval_dir=tmp_path / "approvals", code_fixture_runner=injected,
    )


async def test_selected_policy_assembly_no_docker_start_and_bound_policy_hash(tmp_path):
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps({"image": "sha256:" + "a" * 64}))
    _, deps = await assemble(tmp_path, policy=policy)
    assert isinstance(deps.code_fixture_runner, WorkroomCodeFixtureRunner)
    assert not deps.code_fixture_runner.workroom.known_workrooms()
    assert deps.local_tools.get("code.validate") and deps.local_tools.get("agent.readiness")
    expected = deps.code_fixture_runner.fingerprint
    assert deps.component_binding.code_fixture_fingerprint == expected
    assert specialist_planning_inputs(deps)["code_fixture_runner"] == expected
    policy.write_text(json.dumps({"image": "sha256:" + "b" * 64}))
    _, changed = await assemble(tmp_path, policy=policy)
    assert deps.component_binding != changed.component_binding


async def test_none_retains_legacy_binding_and_injection_uses_same_port(tmp_path):
    _, absent = await assemble(tmp_path)
    assert absent.code_fixture_runner is None
    assert "code_fixture_fingerprint" not in absent.component_binding.model_dump()
    assert "code_fixture_runner" not in specialist_planning_inputs(absent)
    fake = Runner()
    _, selected = await assemble(tmp_path, injected=fake)
    assert selected.code_fixture_runner is fake and not fake.calls
    assert selected.component_binding.code_fixture_fingerprint == fake.fingerprint


@pytest.mark.parametrize("kind", ["missing", "unpinned", "unselected", "ambiguous"])
async def test_invalid_or_ambiguous_configuration_refused_before_gateway_calls(tmp_path, kind):
    policy = tmp_path / "policy.json"
    if kind != "missing":
        policy.write_text(json.dumps({"image": "tag" if kind == "unpinned"
                                     else "sha256:" + "a" * 64}))
    gateway = DummyToolGateway(tools=[])
    with pytest.raises(ValueError):
        await assemble(tmp_path, policy=policy, selected=kind != "unselected",
                       injected=Runner() if kind == "ambiguous" else None, gateway=gateway)
    assert not gateway.invocations


def test_environment_uses_existing_typed_config_boundary():
    config = OrchestrationConfig.from_environment({
        "REDTEAM_CODE_FIXTURE_POLICY_PATH": "operator/fixture.json",
    })
    assert config.code_fixture_policy_path == "operator/fixture.json"
    settings = OrchestrationSettings.from_config(config)
    assert settings.code_fixture_policy_path == config.code_fixture_policy_path
