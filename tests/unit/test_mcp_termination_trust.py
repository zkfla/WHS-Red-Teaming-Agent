"""Only composition-verified providers may assert capacity-releasing termination."""

import pytest

from redteam.capability import bootstrap
from redteam.capability.adapters.mcp.transport import RemoteCallResult, RemoteTool
from redteam.core.contracts.tool import (
    EXECUTION_TERMINATED_METADATA_KEY,
    PROVIDER_RESULT_EXECUTION_TERMINATED,
    PROVIDER_RESULT_EXECUTION_UNCERTAIN,
)
from tests.unit.test_mcp_adapter import FakeTransport, _invocation


@pytest.mark.parametrize("trusted", [False, True])
async def test_gateway_assembly_controls_termination_metadata_trust(monkeypatch, trusted):
    transport = FakeTransport(
        tools=[RemoteTool(name="probe", description="fixture", input_schema={"type": "object"})],
        result=RemoteCallResult(is_error=True, output={"diagnostic": "fixture failure"}, metadata={
            PROVIDER_RESULT_EXECUTION_UNCERTAIN: True,
            PROVIDER_RESULT_EXECUTION_TERMINATED: True,
        }),
    )
    monkeypatch.setattr(bootstrap, "_create_transport", lambda settings: transport)
    gateway = await bootstrap.build_mcp_tool_gateway(
        ["fixture"], env={"REDTEAM_MCP_FIXTURE_KIND": "recon",
                          "REDTEAM_MCP_FIXTURE_ENDPOINT": "https://mcp.invalid/mcp"},
        trusted_termination_servers=("fixture",) if trusted else (),
        health_refresh_enabled=False,
    )
    descriptor, = gateway.all_descriptors()
    invocation = _invocation(tool_id=descriptor.tool_id).model_copy(update={
        "binding_mode": "direct", "operation": None,
    })
    result = await gateway.invoke(invocation)
    assert result.metadata["execution_uncertain"] is True
    assert (result.metadata.get(EXECUTION_TERMINATED_METADATA_KEY) is True) is trusted
    assert len(transport.calls) == 1


async def test_unregistered_termination_trust_is_rejected():
    with pytest.raises(ValueError, match="termination trust requires a registered provider"):
        await bootstrap.build_mcp_tool_gateway([], trusted_termination_servers=("missing",))
