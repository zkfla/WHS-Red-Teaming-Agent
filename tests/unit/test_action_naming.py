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
        ("web", "frida_attach"),
    ],
)
def test_noncanonical_action_names_fail_closed(
    capability: str, operation: str
) -> None:
    with pytest.raises(ValueError):
        canonical_action_key(capability, operation)


@pytest.mark.parametrize("tool_id", ["recon", "recon.nmap.scan", ".nmap", "recon."])
def test_tool_id_requires_exactly_namespace_and_remote_name(tool_id: str) -> None:
    with pytest.raises(ValueError):
        tool_namespace(ToolId(tool_id))
