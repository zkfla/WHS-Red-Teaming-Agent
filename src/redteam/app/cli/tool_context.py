"""Conversation projections of declared versus discovered capabilities; no execution."""

from redteam.app.bootstrap import EXAMPLE_MCP_SENTINEL
from redteam.app.cli.conversation import catalog_tool_labels
from redteam.capability.settings import (
    MCPServerSettings,
    automatic_shell_setup_from_environment,
    gated_network_policy_path_from_environment,
    shell_policy_path_from_environment,
)
from redteam.core.contracts.mission import redact_secret_material


def live_tool_context(catalog, engagement=None):
    availability: dict = {"state": "discovered", "servers": []}
    if catalog is None:
        availability["state"] = "not_inspected"
    else:
        # Reading a finalized catalog never opens a transport or executes a tool.
        availability["servers"] = [
            {"server_id": descriptor.server_id, "tool_name": descriptor.tool_name,
             "health": descriptor.health_status.value}
            for descriptor in catalog.all()[:80]
        ]
    if engagement is not None:
        metadata = getattr(engagement, "metadata", {})
        availability["constraints"] = redact_secret_material({
            "mcp_targets": metadata.get("mcp_targets", {}),
            "web_policy": metadata.get("web_policy", {}),
        })
    return {"tools": catalog_tool_labels(catalog), "tool_availability": availability}


def configured_tool_context(config):
    """Before a Run, advertise selections honestly without preparing Docker/MCP."""
    servers = []
    for identity in config.capability.mcp_servers:
        if identity == EXAMPLE_MCP_SENTINEL:
            servers.append({"server_id": identity, "state": "explicit_test_fixture"})
            continue
        try:
            settings = MCPServerSettings.from_environment(identity)
            servers.append({"server_id": identity, "state": "configured_unverified",
                            "allowed_tool_names": list(settings.allowed_tools or [])})
        except ValueError:
            servers.append({"server_id": identity, "state": "invalid_configuration"})
    try:
        shell = "prepare_at_run_start" if automatic_shell_setup_from_environment() else (
            "configured_unverified" if shell_policy_path_from_environment() else "disabled"
        )
        kali_network = "configured_unverified" if gated_network_policy_path_from_environment() \
            else "closed"
    except ValueError:
        shell, kali_network = "invalid_configuration", "invalid_configuration"
    return {"tools": [], "tool_availability": {
        "state": "configured_unverified", "servers": servers,
        "shell": {"state": shell, "target_network": kali_network},
        "constraints": redact_secret_material({
            "mcp_targets": config.engagement.metadata.get("mcp_targets", {}),
            "web_policy": config.engagement.metadata.get("web_policy", {}),
        }),
    }}
