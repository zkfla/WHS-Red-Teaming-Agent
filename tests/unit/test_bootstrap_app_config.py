"""AppConfig -> bootstrap wiring test.

Covers: every AppConfig section reaches its component, and every
dependency ``build_application`` would otherwise build can be
overridden with a Fake (instruction.md section 1/21 of the follow-up
brief).
"""

from __future__ import annotations

from redteam.app.bootstrap import build_application, run_engagement
from redteam.core.config import load_app_config
from redteam.core.enums import AgentPhase, PolicyVerdict, Verdict
from tests.fakes import DummyToolGateway, FakeControlPlane, FakeEventBus, FakeEvidenceStore


async def test_build_application_from_app_config_runs_end_to_end(tmp_path) -> None:
    config = load_app_config("examples/app.yaml")

    engagement, deps = await build_application(config, evidence_dir=tmp_path)

    assert engagement.name == config.engagement.name
    # config.llm.provider == "fake" in examples/app.yaml -> FakeLLMClient chosen.
    assert deps.llm_client.__class__.__name__ == "FakeLLMClient"

    final_state = await run_engagement(
        engagement, deps=deps, orchestration_config=config.orchestration
    )
    assert final_state.phase == AgentPhase.FINISHED
    # OrchestrationConfig.max_steps reached RuntimeState.remaining_budget.
    assert final_state.remaining_budget == config.orchestration.max_steps


async def test_build_application_accepts_fake_overrides_for_every_port(tmp_path) -> None:
    config = load_app_config("examples/app.yaml")

    fake_control_plane = FakeControlPlane(default_verdict=PolicyVerdict.ALLOW)
    fake_tool_gateway = DummyToolGateway()
    fake_evidence_store = FakeEvidenceStore()
    fake_event_bus = FakeEventBus()

    engagement, deps = await build_application(
        config,
        control_plane=fake_control_plane,
        tool_gateway=fake_tool_gateway,
        evidence_store=fake_evidence_store,
        event_bus=fake_event_bus,
        evidence_dir=tmp_path,
    )

    assert deps.control_plane is fake_control_plane
    assert deps.tool_gateway is fake_tool_gateway
    assert deps.evidence_store is fake_evidence_store
    assert deps.event_bus is fake_event_bus

    final_state = await run_engagement(engagement, deps=deps)
    assert final_state.phase == AgentPhase.FINISHED
    assert final_state.last_verdict == Verdict.PASS
    assert len(fake_control_plane.evaluated) == 1
    assert len(fake_tool_gateway.invocations) == 1
