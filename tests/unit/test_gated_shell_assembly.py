"""Configured gated descriptors reach the real catalog before Control is built."""

import pytest

from redteam.app.bootstrap import build_application
from redteam.capability.gated_activation import GatedProviderConfig
from redteam.capability.gated_manifest import build_gated_descriptors
from redteam.capability.gated_network_policy import GatedNetworkPolicy
from redteam.capability.shell_runtime import build_shell_runtime
from redteam.core.config import load_app_config
from redteam.core.contracts.network import (
    NetworkBudget,
    NetworkCapacity,
    NetworkPurpose,
    WindowCaps,
)
from redteam.core.ids import EngagementId
from redteam.llm.fake import FakeLLMClient
from redteam.orchestration.settings import OrchestrationSettings
from tests.unit.test_shell_runtime_008 import _policy_file


async def test_operator_configuration_rebuilds_same_catalog_without_opening_windows(tmp_path):
    config = load_app_config("examples/app.yaml")
    settings = OrchestrationSettings(task_graph_path=str(tmp_path / "tasks.sqlite3"))
    capacity = NetworkCapacity(max_flow_init_deliveries=4, max_output_bytes=4096)
    caps = dict(max_flow_init_deliveries=2, max_output_bytes=2048, duration_seconds=60)
    provider_config = GatedProviderConfig(network_policy=GatedNetworkPolicy(
        budget=NetworkBudget(
            global_capacity=capacity, target_capacity=capacity, install_capacity=capacity,
        ),
        default_target_caps=WindowCaps(purpose=NetworkPurpose.TARGET, **caps),
        default_install_caps=WindowCaps(purpose=NetworkPurpose.INSTALL, **caps),
    ))
    expected = {str(d.tool_id) for d in build_gated_descriptors(None)}
    async def assemble(shell, provider_config):
        return await build_application(
            config, llm=FakeLLMClient(), orchestration_settings=settings,
            engagement_id=EngagementId("assembly-engagement"),
            builtin_adapters=[shell.adapter], builtin_concurrency=shell.concurrency,
            builtin_output_limits=shell.output_limits, shell_programs=shell.program_source,
            gated_shell=(shell, provider_config),
            evidence_dir=tmp_path / "evidence", audit_dir=tmp_path / "audit",
            approval_dir=tmp_path / "approvals",
        )

    for _ in range(2):
        shell = build_shell_runtime(_policy_file(tmp_path))
        assert shell is not None
        _, deps = await assemble(shell, provider_config)
        assert deps.tool_catalog is not None
        actual = {str(d.tool_id) for d in deps.tool_catalog.all_descriptors()}
        assert expected <= actual
        assert len(actual) == 7
    assert (tmp_path / "network_grants.sqlite3").is_file()
    # Rebuilding the application cannot widen the same engagement's
    # durable network budget, even with a fresh adapter instance.
    shell = build_shell_runtime(_policy_file(tmp_path))
    assert shell is not None
    widened = provider_config.network_policy.model_copy(update={
        "budget": provider_config.network_policy.budget.model_copy(update={
            "global_capacity": NetworkCapacity(
                max_flow_init_deliveries=8, max_output_bytes=8192,
            ),
        }),
    })
    with pytest.raises(ValueError, match="persisted network budget differs"):
        await assemble(shell, GatedProviderConfig(network_policy=widened))
    assert len(await shell.adapter.discover_tools()) == 4
