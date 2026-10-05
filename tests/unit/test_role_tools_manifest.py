"""Role files select actual discovered tools and pin the effective resume view."""

from __future__ import annotations

import json

import pytest

from redteam.capability.catalog import MCPToolCatalog
from redteam.capability.registry import MCPToolRegistry
from redteam.capability.role_manifest import configure_role_tools, role_tool_visible, tools_for_role
from redteam.core.contracts.tool import ToolDescriptor
from redteam.core.ids import AgentId, ToolId
from redteam.orchestration.agents.definition import AgentDefinition, ContextProfile
from redteam.orchestration.agents.role_view import (
    role_tool_visible as runtime_tool_visible,
)
from redteam.orchestration.agents.role_view import (
    tools_for_role as runtime_tools_for_role,
)


def _agent(agent_id="specialist", selection=None, capabilities=None):
    return AgentDefinition(
        agent_id=AgentId(agent_id),
        role="specialist",
        name="Specialist",
        provider_id="package:specialist",
        context_profile=ContextProfile(profile_id="specialist"),
        capabilities=capabilities or ["analysis"],
        tool_selection=selection,
    )


def _catalog():
    registry = MCPToolRegistry()
    for namespace, server, name, tags in [
        ("analysis", "reader", "read", ["analysis"]),
        ("analysis", "other", "inspect", ["analysis"]),
        ("web", "web", "fetch", ["web"]),
    ]:
        registry.register(
            ToolDescriptor(
                tool_id=ToolId(f"{namespace}.{server}.{name}"),
                server_id=server,
                tool_name=name,
                capability_tags=tags,
            )
        )
    return MCPToolCatalog(registry, {})


def _file(tmp_path, name, rows):
    path = tmp_path / name
    path.write_text(json.dumps({"roles": rows}), encoding="utf-8")
    return path


def test_file_override_alias_pins_the_same_view_after_file_changes(tmp_path):
    catalog = _catalog()
    first = _agent(selection={"server_ids": ["other"]})
    path = _file(
        tmp_path,
        "roles.yaml",
        [{"agent_id": "specialist", "tool_ids": ["analysis.read", "analysis.read"]}],
    )
    fingerprint = configure_role_tools([first], catalog, path)
    assert first.tool_selection["tool_ids"] == ["analysis.reader.read"]
    assert [str(tool.tool_id) for tool in tools_for_role(first, catalog)] == [
        "analysis.reader.read"
    ]
    assert all(
        role_tool_visible(first, tool) == (tool.server_id == "reader")
        for tool in catalog.all_descriptors()
    )
    assert all(not tool.operations for tool in tools_for_role(first, catalog))
    # Consumers use the injected snapshot; editing the source does not widen it.
    path.write_text('{"roles":[]}', encoding="utf-8")
    assert len(tools_for_role(first, catalog)) == 1
    assert runtime_tools_for_role(first, catalog) == tools_for_role(first, catalog)
    assert all(
        runtime_tool_visible(first, tool) == role_tool_visible(first, tool)
        for tool in catalog.all_descriptors()
    )
    second = _agent()
    same = _file(
        tmp_path, "same.json", [{"agent_id": "specialist", "tool_ids": ["analysis.reader.read"]}]
    )
    assert configure_role_tools([second], catalog, same) == fingerprint
    changed = _file(tmp_path, "changed.json", [{"agent_id": "specialist", "server_ids": ["other"]}])
    assert configure_role_tools([_agent()], catalog, changed) != fingerprint


def test_omitted_roles_keep_package_or_capability_defaults_and_empty_blocks(tmp_path):
    catalog = _catalog()
    package = _agent("package-role", {"server_ids": ["reader"]})
    omitted = _agent("new-role")
    blocked = _agent("blocked-role", {"server_ids": ["other"]})
    builtin = _agent("recon-agent", capabilities=["web"])
    path = _file(tmp_path, "empty.json", [{"agent_id": "blocked-role"}])
    configure_role_tools([package, omitted, blocked, builtin], catalog, path)
    assert [tool.server_id for tool in tools_for_role(package, catalog)] == ["reader"]
    assert {tool.server_id for tool in tools_for_role(omitted, catalog)} == {"reader", "other"}
    assert tools_for_role(blocked, catalog) == []
    assert tools_for_role(builtin, catalog) == []  # package YAML recon tags, not web
    for configured in (package, omitted, blocked, builtin):
        assert runtime_tools_for_role(configured, catalog) == tools_for_role(configured, catalog)


def test_selectors_and_dimensions_or_members_and_order_independent_digest(tmp_path):
    catalog = _catalog()
    row = {
        "agent_id": "specialist",
        "server_ids": ["reader", "web"],
        "capability_tags": ["analysis"],
        "methodology_refs": ["guide", "guide"],
    }
    path = _file(tmp_path, "one.json", [row])
    agent = _agent()
    fingerprint = configure_role_tools([agent], catalog, path)
    assert [tool.server_id for tool in tools_for_role(agent, catalog)] == ["reader"]
    row.update(server_ids=["web", "reader", "reader"], methodology_refs=["guide"])
    same = _file(tmp_path, "two.json", [row])
    assert configure_role_tools([_agent()], catalog, same) == fingerprint


@pytest.mark.parametrize("catalog_available", [False, True])
def test_unknown_explicit_tool_is_configuration_error(tmp_path, catalog_available):
    path = _file(
        tmp_path, "bad.json", [{"agent_id": "specialist", "tool_ids": ["analysis.missing"]}]
    )
    with pytest.raises(ValueError, match="unregistered tools|no catalog"):
        configure_role_tools([_agent()], _catalog() if catalog_available else None, path)


def test_readonly_assembly_injects_non_tool_selectors_but_exposes_empty_view(tmp_path):
    agent = _agent()
    path = _file(tmp_path, "readonly.json", [{"agent_id": "specialist", "server_ids": ["reader"]}])
    assert configure_role_tools([agent], None, path)
    assert agent.tool_selection["server_ids"] == ["reader"]
    assert tools_for_role(agent, None) == []
