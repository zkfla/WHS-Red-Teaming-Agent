"""AppConfig -> bootstrap wiring test.

Covers: every AppConfig section reaches its component, and every
dependency ``build_application`` would otherwise build can be
overridden with a Fake (instruction.md section 1/21 of the follow-up
brief).
"""

from __future__ import annotations

from redteam.app.bootstrap import build_application, run_engagement
from redteam.capability.bootstrap import build_mcp_tool_gateway
from redteam.core.config import load_app_config
from redteam.core.enums import AgentPhase, PolicyVerdict, Verdict
from redteam.core.ids import ToolId
from redteam.orchestration.settings import OrchestrationSettings
from redteam.orchestration.task_graph.sqlite import SQLiteTaskGraphStore
from redteam.orchestration.work.sqlite import SQLiteWorkStore
from tests.fakes import DummyToolGateway, FakeControlPlane, FakeEventBus, FakeEvidenceStore


async def test_build_application_from_app_config_runs_end_to_end(tmp_path) -> None:
    config = load_app_config("examples/app.yaml")
    # Control now enforces ROE time windows; this wiring test is clock-independent.
    for rule in config.engagement.rules_of_engagement:
        rule.allowed_time_windows = []

    # examples/app.yaml deliberately ships `capability.mcp_servers: []`, and
    # build_application() itself no longer substitutes ExampleMCPAdapter for
    # an empty list (production must never do that silently -- see
    # `app.bootstrap._empty_tool_gateway`). This end-to-end wiring test
    # still wants one real, executable tool, so it asks for the same
    # in-process example adapter explicitly, the same way any other caller
    # now has to.
    example_gateway = await build_mcp_tool_gateway([])

    engagement, deps = await build_application(
        config,
        tool_gateway=example_gateway,
        tool_catalog=example_gateway,
        evidence_dir=tmp_path / "evidence",
        audit_dir=tmp_path / "audit",
        approval_dir=tmp_path / "approvals",
    )

    # All three teams must share the configured catalog/execution gateway.
    assert deps.tool_catalog is deps.tool_gateway
    assert deps.tool_catalog is not None
    assert deps.tool_catalog.get(ToolId("recon.ping")) is not None
    assert deps.tool_catalog.list_by_capability("recon")

    assert engagement.name == config.engagement.name
    # config.llm.provider == "fake" in examples/app.yaml -> FakeLLMClient chosen.
    assert deps.llm_client.__class__.__name__ == "FakeLLMClient"

    final_state = await run_engagement(
        engagement, deps=deps, orchestration_config=config.orchestration
    )
    assert final_state.phase == AgentPhase.FINISHED
    assert final_state.actions_executed > 0
    assert list((tmp_path / "audit").glob("*.jsonl"))
    assert await deps.evidence_store.list_for_run(final_state.run_id)
    # OrchestrationConfig now reaches Orchestration as the loop's turn
    # ceiling (OrchestrationSettings.max_steps), which is what it is
    # named for; the run finished well inside it.
    assert 0 < final_state.step_count <= config.orchestration.max_steps
    # RuntimeState.remaining_budget tracks the engagement's *action*
    # budget and is decremented per executed action, so it is a real
    # stop condition rather than a constant copied in at startup.
    assert final_state.remaining_budget == (
        config.engagement.execution_budget.max_actions - final_state.actions_executed
    )


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


async def test_build_application_can_wire_the_durable_task_graph(tmp_path) -> None:
    config = load_app_config("examples/app.yaml")
    settings = OrchestrationSettings(
        checkpointing=False,
        task_graph_backend="sqlite",
        task_graph_path=str(tmp_path / "task-graph.sqlite3"),
    )

    _, deps = await build_application(
        config,
        evidence_dir=tmp_path / "evidence",
        orchestration_settings=settings,
    )

    assert isinstance(deps.task_graph_store, SQLiteTaskGraphStore)


async def test_build_application_can_wire_the_durable_work_ledger(tmp_path) -> None:
    config = load_app_config("examples/app.yaml")
    settings = OrchestrationSettings(
        checkpointing=False,
        work_backend="sqlite",
        work_path=str(tmp_path / "work.sqlite3"),
    )

    _, deps = await build_application(
        config,
        evidence_dir=tmp_path / "evidence",
        orchestration_settings=settings,
    )

    assert isinstance(deps.work_store, SQLiteWorkStore)
    assert deps.settings is settings
