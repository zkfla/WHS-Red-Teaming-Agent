import pytest

from redteam.core.ids import ToolId
from redteam.orchestration.action_naming import (
    canonical_action_key,
    tool_matches_action_namespace,
    tool_namespace,
)


def test_canonical_action_key_keeps_intent_separate_from_tool_id() -> None:
    assert canonical_action_key("recon", "port_scan") == "recon.port_scan"
    assert tool_namespace(ToolId("recon.nmap_scan")) == "recon"
    assert tool_matches_action_namespace(ToolId("recon.nmap_scan"), "recon") is True


@pytest.mark.parametrize(
    ("capability", "operation"),
    [
        ("recon.port_scan", "port_scan"),
        ("WEB", "http_request"),
        ("web", "NmapScan"),
    ],
)
def test_noncanonical_action_names_fail_closed(capability: str, operation: str) -> None:
    with pytest.raises(ValueError):
        canonical_action_key(capability, operation)


def test_new_operation_names_are_formatted_independently_of_tool_support() -> None:
    assert canonical_action_key("web", "frida_attach") == "web.frida_attach"


@pytest.mark.parametrize(
    "tool_id", ["recon", "recon..scan", "recon.srv.tool.extra", ".nmap", "recon."]
)
def test_tool_id_requires_a_complete_legacy_or_physical_identity(tool_id: str) -> None:
    with pytest.raises(ValueError):
        tool_namespace(ToolId(tool_id))


def test_physical_tool_namespace_preserves_execution_binding() -> None:
    assert tool_namespace(ToolId("recon.server.scan")) == "recon"
    assert tool_matches_action_namespace(ToolId("recon.server.scan"), "recon")
    assert not tool_matches_action_namespace(ToolId("recon.server.scan"), "web")
