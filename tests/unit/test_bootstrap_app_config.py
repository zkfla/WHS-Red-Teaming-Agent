"""AppConfig -> bootstrap wiring test (explicit test-LLM injection).

Covers: every AppConfig section reaches its component, and every
dependency ``build_application`` would otherwise build can be
overridden with a Fake (instruction.md section 1/21 of the follow-up
brief).
"""

from __future__ import annotations

import pytest

from redteam.app.bootstrap import build_application, build_llm_client
from redteam.capability.bootstrap import build_mcp_tool_gateway
from redteam.core.config import LLMConfig, load_app_config
from redteam.core.enums import PolicyVerdict
from redteam.core.ids import ToolId
from redteam.llm.fake import FakeLLMClient
from redteam.orchestration.settings import OrchestrationSettings
from redteam.orchestration.task_graph.sqlite import SQLiteTaskGraphStore
from redteam.orchestration.work.sqlite import SQLiteWorkStore
from tests.fakes import DummyToolGateway, FakeControlPlane, FakeEventBus, FakeEvidenceStore


async def test_build_application_wires_real_catalog_with_explicit_test_llm(tmp_path) -> None:
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

    fake_llm = FakeLLMClient()
    engagement, deps = await build_application(
        config,
        tool_gateway=example_gateway,
        tool_catalog=example_gateway,
        llm=fake_llm,
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
    assert deps.llm_client is fake_llm
    assert hasattr(deps.tool_gateway, "invoke")
    assert engagement.execution_budget.max_actions == config.engagement.execution_budget.max_actions
    assert deps.settings.max_steps == config.orchestration.max_steps


async def test_build_application_accepts_fake_overrides_for_every_port(tmp_path) -> None:
    config = load_app_config("examples/app.yaml")

    fake_control_plane = FakeControlPlane(default_verdict=PolicyVerdict.ALLOW)
    fake_tool_gateway = DummyToolGateway()
    fake_evidence_store = FakeEvidenceStore()
    fake_event_bus = FakeEventBus()

    fake_llm = FakeLLMClient()
    engagement, deps = await build_application(
        config,
        control_plane=fake_control_plane,
        tool_gateway=fake_tool_gateway,
        evidence_store=fake_evidence_store,
        event_bus=fake_event_bus,
        llm=fake_llm,
        evidence_dir=tmp_path,
    )

    assert deps.control_plane is fake_control_plane
    assert deps.tool_gateway is fake_tool_gateway
    assert deps.evidence_store is fake_evidence_store
    assert deps.event_bus is fake_event_bus

    assert deps.llm_client is fake_llm
    assert engagement.name == config.engagement.name


async def test_build_application_can_wire_the_durable_task_graph(tmp_path) -> None:
    config = load_app_config("examples/app.yaml")
    settings = OrchestrationSettings(
        checkpointing=False,
        task_graph_backend="sqlite",
        task_graph_path=str(tmp_path / "task-graph.sqlite3"),
    )

    _, deps = await build_application(
        config,
        llm=FakeLLMClient(),
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
        llm=FakeLLMClient(),
        evidence_dir=tmp_path / "evidence",
        orchestration_settings=settings,
    )

    assert isinstance(deps.work_store, SQLiteWorkStore)
    assert deps.settings is settings


def test_build_llm_client_rejects_fake_provider() -> None:
    with pytest.raises(ValueError, match="real LLM provider"):
        build_llm_client(LLMConfig(provider="fake"))


def test_llm_config_requires_explicit_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REDTEAM_LLM_PROVIDER", raising=False)
    with pytest.raises(ValueError, match="REDTEAM_LLM_PROVIDER"):
        LLMConfig.from_environment()
