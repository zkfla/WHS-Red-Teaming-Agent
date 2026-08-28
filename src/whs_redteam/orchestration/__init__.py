from whs_redteam.orchestration.adapters import (
    LocalProbeAdapter,
    MCPProbeAdapter,
    MCPToolBinding,
    ProbeRegistry,
    ToolAdapter,
)
from whs_redteam.orchestration.dispatcher import ActionDispatcher
from whs_redteam.orchestration.mcp_transport import OfficialMCPTransport

# Keep the coordinator import lazy.  The injection probes depend on the
# adapter protocol, and importing AgentOrchestrator while this package is
# initialising creates a cycle (injection -> adapters -> orchestration package
# -> orchestrator -> injection).  ``from whs_redteam.orchestration import
# AgentOrchestrator`` remains supported through ``__getattr__`` below.


def __getattr__(name: str):
    if name == "AgentOrchestrator":
        from whs_redteam.orchestration.orchestrator import AgentOrchestrator

        return AgentOrchestrator
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

__all__ = [
    "ActionDispatcher",
    "AgentOrchestrator",
    "LocalProbeAdapter",
    "MCPProbeAdapter",
    "MCPToolBinding",
    "OfficialMCPTransport",
    "ProbeRegistry",
    "ToolAdapter",
]
