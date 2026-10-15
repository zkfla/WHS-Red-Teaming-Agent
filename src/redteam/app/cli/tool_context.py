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
    gated = _gated_network_projection(catalog)
    availability["gated_network"] = gated
    return {"tools": catalog_tool_labels(catalog), "tool_availability": availability}


def _gated_network_projection(catalog):
    """Project P readiness from the assembled capability catalog only."""
    from redteam.capability.gated_manifest import gated_operation_for

    descriptors = catalog.all() if catalog is not None else []
    prepared = any(gated_operation_for(descriptor.tool_name) is not None
                   for descriptor in descriptors)
    return {
        "state": "network_prerequisites_ready" if prepared else "local_only",
        "route_and_roe": "pending" if prepared else "closed",
        "target_ready": False,
    }


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
    web_policy = config.engagement.metadata.get("web_policy", {})
    selected_mode = web_policy.get("kali_network_mode") if isinstance(web_policy, dict) else None
    try:
        shell = "prepare_at_run_start" if automatic_shell_setup_from_environment() else (
            "configured_unverified" if shell_policy_path_from_environment() else "disabled"
        )
        kali_network = (
            "prepare_at_run_start" if selected_mode == "prepare" else
            "local_only" if selected_mode == "closed" else
            "configured_unverified" if selected_mode == "configured"
            or gated_network_policy_path_from_environment() else "local_only"
        )
    except ValueError:
        shell, kali_network = "invalid_configuration", "invalid_configuration"
    return {"tools": [], "tool_availability": {
        "state": "configured_unverified", "servers": servers,
        "shell": {"state": shell, "target_network": kali_network},
        "gated_network": {
            "state": kali_network,
            "route_and_roe": "pending" if kali_network in {
                "prepare_at_run_start", "configured_unverified",
            } else "closed",
            "target_ready": False,
        },
        "constraints": redact_secret_material({
            "mcp_targets": config.engagement.metadata.get("mcp_targets", {}),
            "web_policy": config.engagement.metadata.get("web_policy", {}),
        }),
    }}
