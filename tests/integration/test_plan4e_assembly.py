"""Selected code stages use separate immutable storage and durable identities."""

import shutil
from pathlib import Path

import yaml

from redteam.app.central_run import open_central_run
from redteam.app.central_views import inspect_central_run
from redteam.app.cli.commands.validate import check_execution_configuration
from redteam.core.config import AppConfig, EngagementConfig, OrchestrationConfig
from redteam.evidence.artifacts.filesystem import FilesystemArtifactBackend
from redteam.llm.fake import FakeLLMClient
from tests.integration.test_central_completion import OriginalProtocolDouble, selection
from tests.integration.test_central_development import engagement, gateway
from tests.integration.test_central_parallel import limits


async def test_selected_code_stages_are_registered_stored_and_reported(tmp_path):
    async with selection(tmp_path) as (session, llm):
        deps = session.deps
        assert deps.artifact_store is not None
        assert deps.code_fixture_runner is None
        assert {"code.prepare", "code.inspect", "code.validate"} <= {
            tool.tool_id for tool in deps.local_tools.descriptors()
        }
        stored = await deps.artifact_store.put(b"synthetic artifact", "text/plain")
        assert await deps.artifact_store.verify(stored.artifact_ref, stored.digest)
        assert await deps.artifact_store.get(stored.artifact_ref) == b"synthetic artifact"
        paths = deps.central_state.original_store_paths
        assert Path(paths["artifacts"]) == tmp_path / "run" / "artifacts"
        assert paths["artifacts"] != paths["evidence"]
        _, original, report = await inspect_central_run(
            deps.central_state.path, session.runtime.run_id,
        )
        assert (
            original["code_features"]["artifact_store_fingerprint"]
            == deps.artifact_store.fingerprint
        )
        assert report.feature_availability["code_artifacts"] == "enabled"
        assert report.feature_availability["code_fixtures"] == "unconfigured"
        assert llm.requests == []


async def test_missing_stage_pair_disables_artifacts_in_assembly_and_preflight(tmp_path):
    profile = Path(__file__).parents[2] / "config" / "agents"
    shutil.copytree(profile, tmp_path / "agents")
    path = tmp_path / "agents" / "native-specialists.yaml"
    raw = yaml.safe_load(path.read_text())
    raw["stages"].pop("code_generation")
    raw["stages"].pop("code_review")
    path.write_text(yaml.safe_dump(raw))
    settings = limits()[1].with_overrides(specialist_profile_path=str(path))
    config = AppConfig(engagement=EngagementConfig.model_validate(
        engagement().model_dump(exclude={"engagement_id", "created_at"}),
    ), orchestration=OrchestrationConfig(
        specialist_profile_path=str(path), llm_request_policy=settings.llm_request_policy,
    ))
    tools = OriginalProtocolDouble(gateway())
    async with open_central_run(
        config, directory=tmp_path / "run", settings=settings, llm=FakeLLMClient(),
        tool_gateway=tools, tool_catalog=tools.catalog,
    ) as session:
        assert session.deps.artifact_store is None
        assert session.deps.local_tools.get("code.prepare") is None
        assert not (tmp_path / "run" / "artifacts").exists()
        assert "artifacts" not in session.deps.central_state.original_store_paths
    checks = check_execution_configuration(config)
    assert ("Code artifacts (disabled)", True, []) in checks


def test_artifact_location_and_policy_have_stable_distinct_identity(tmp_path):
    first = FilesystemArtifactBackend(tmp_path / "a")
    assert first.fingerprint == FilesystemArtifactBackend(tmp_path / "a").fingerprint
    assert first.fingerprint != FilesystemArtifactBackend(tmp_path / "b").fingerprint
    changed_policy = FilesystemArtifactBackend(tmp_path / "a", max_size_bytes=100)
    assert first.fingerprint != changed_policy.fingerprint


async def test_originally_disabled_feature_retains_original_binding_on_resume(tmp_path):
    config = AppConfig(engagement=EngagementConfig.model_validate(
        engagement().model_dump(exclude={"engagement_id", "created_at"}),
    ))
    settings = limits()[1]
    tools = OriginalProtocolDouble(gateway())
    options = {
        "directory": tmp_path / "run", "settings": settings, "llm": FakeLLMClient(),
        "tool_gateway": tools, "tool_catalog": tools.catalog, "include_code_artifacts": False,
    }
    async with open_central_run(config, **options) as session:
        run_id = session.runtime.run_id
        engagement_id = session.runtime.engagement_id
        original_binding = session.deps.component_binding
        assert session.deps.local_tools.get("code.prepare") is None
        assert "artifacts" not in session.deps.central_state.original_store_paths
        _, original, report = await inspect_central_run(session.deps.central_state.path, run_id)
        assert "code_features" not in original
        assert report.feature_availability["code_artifacts"] == "disabled"
    async with open_central_run(
        config, **options, run_id=run_id, engagement_id=engagement_id, resume=True,
    ) as session:
        assert session.deps.component_binding == original_binding
        assert session.deps.artifact_store is None
        assert session.deps.local_tools.get("code.prepare") is None
