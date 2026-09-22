"""``ResultMapperRegistry`` and ``map_recon_http_probe`` in isolation --
no graph, no orchestration state, just the pure mapping function.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from redteam.core.contracts.target import EndpointTarget, HostTarget
from redteam.core.contracts.tool import ToolError, ToolResult
from redteam.core.enums import GraphEdgeType, GraphNodeType
from redteam.core.ids import ActionId, InvocationId, ToolId
from redteam.orchestration.observations import (
    GraphMutation,
    ResultMapperRegistry,
    default_result_mapper_registry,
)
from redteam.orchestration.observations.recon_http import map_recon_http_probe

NOW = datetime.now(timezone.utc)


def _result(
    success: bool = True, output: dict | str | None = None, tool_id: str = "recon.http_probe"
) -> ToolResult:
    return ToolResult(
        invocation_id=InvocationId("inv_1"),
        action_id=ActionId("action_1"),
        tool_id=ToolId(tool_id),
        success=success,
        output=output,
        error=None if success else ToolError(code="timeout", message="no answer"),
        started_at=NOW,
        finished_at=NOW,
    )


_GOOD_OUTPUT = {
    "target_ref": "x",
    "status_code": 200,
    "redirect": False,
    "security_headers_present": {"strict-transport-security": False},
}


def test_maps_a_successful_probe_to_service_and_endpoint() -> None:
    mutation = map_recon_http_probe(
        target=HostTarget(host="a.local"),
        arguments={"path": "/"},
        result=_result(output=_GOOD_OUTPUT),
    )

    assert {n.node_type for n in mutation.nodes} == {GraphNodeType.SERVICE, GraphNodeType.ENDPOINT}
    assert {e.edge_type for e in mutation.edges} == {GraphEdgeType.HOSTS, GraphEdgeType.EXPOSES}


def test_defaults_path_to_root_when_argument_missing() -> None:
    mutation = map_recon_http_probe(
        target=HostTarget(host="a.local"), arguments={}, result=_result(output=_GOOD_OUTPUT)
    )
    endpoint = next(n for n in mutation.nodes if n.node_type is GraphNodeType.ENDPOINT)
    assert endpoint.facts["path"] == "/"


@pytest.mark.parametrize(
    "result_kwargs",
    [
        {"success": False, "output": _GOOD_OUTPUT},
        {"success": True, "output": "not a dict"},
        {"success": True, "output": None},
        {"success": True, "output": {"no_status_code": True}},
        {"success": True, "output": {"status_code": "200"}},  # wrong type, not coerced
    ],
)
def test_returns_empty_mutation_for_anything_not_a_confirmed_response(result_kwargs) -> None:  # noqa: ANN001
    mutation = map_recon_http_probe(
        target=HostTarget(host="a.local"), arguments={"path": "/"}, result=_result(**result_kwargs)
    )
    assert mutation == GraphMutation()


def test_returns_empty_mutation_for_a_non_host_target() -> None:
    mutation = map_recon_http_probe(
        target=EndpointTarget(host="a.local", path="/x"),
        arguments={"path": "/"},
        result=_result(output=_GOOD_OUTPUT),
    )
    assert mutation == GraphMutation()


def test_never_invents_a_port_because_the_tool_output_never_carries_one() -> None:
    mutation = map_recon_http_probe(
        target=HostTarget(host="a.local"),
        arguments={"path": "/"},
        result=_result(output=_GOOD_OUTPUT),
    )
    service = next(n for n in mutation.nodes if n.node_type is GraphNodeType.SERVICE)
    assert "port" not in service.facts
    assert "port" not in service.attrs
    assert ":80" not in str(service.node_id)


# -- registry --------------------------------------------------------------


def test_registry_returns_empty_mutation_for_an_unregistered_tool() -> None:
    registry = ResultMapperRegistry()
    mutation = registry.map_result(
        tool_id=ToolId("web.http_inspect"),
        target=HostTarget(host="a.local"),
        arguments={},
        result=_result(tool_id="web.http_inspect", output=_GOOD_OUTPUT),
    )
    assert mutation == GraphMutation()


def test_registry_swallows_a_mapper_exception() -> None:
    def _broken(*, target, arguments, result):  # noqa: ANN001
        raise RuntimeError("boom")

    registry = ResultMapperRegistry()
    registry.register("recon.broken", _broken)

    mutation = registry.map_result(
        tool_id=ToolId("recon.broken"),
        target=HostTarget(host="a.local"),
        arguments={},
        result=_result(tool_id="recon.broken", output=_GOOD_OUTPUT),
    )
    assert mutation == GraphMutation()


def test_registry_refuses_a_duplicate_registration() -> None:
    registry = ResultMapperRegistry()
    registry.register("recon.custom", map_recon_http_probe)
    with pytest.raises(ValueError, match="already registered"):
        registry.register("recon.custom", map_recon_http_probe)


def test_http_mapping_preserves_https_port_and_literal_path() -> None:
    mutation = map_recon_http_probe(
        target=HostTarget(host="a.local"), arguments={"path": "/accounts/1234"},
        result=_result(output={**_GOOD_OUTPUT, "host": "a.local", "port": 8443,
                               "scheme": "https"}),
    )
    endpoint = next(n for n in mutation.nodes if n.node_type is GraphNodeType.ENDPOINT)
    assert endpoint.facts["scheme"] == "https"
    assert endpoint.facts["port"] == 8443
    assert endpoint.facts["observed_target"] == "/accounts/1234"
    assert ":8443" in endpoint.node_id
    assert endpoint.node_id.endswith("/accounts/{id}")


@pytest.mark.parametrize("extra", [
    {"host": "other.local"}, {"port": True}, {"port": 70000},
    {"status_code": True}, {"scheme": "ftp"},
])
def test_http_mapping_rejects_invalid_identity(extra: dict) -> None:
    assert map_recon_http_probe(
        target=HostTarget(host="a.local"), arguments={},
        result=_result(output={**_GOOD_OUTPUT, **extra}),
    ) == GraphMutation()


@pytest.mark.parametrize(("tool", "output", "fact"), [
    ("dns_resolve", {"addresses": ["127.0.0.1", "bad", "127.0.0.1"]},
     "resolved_addresses"),
    ("openapi_inventory", {"operations": [{"path": "/accounts/{id}", "method": "GET"}]},
     "operations_candidates"),
    ("html_inventory", {"references": [{"path": "/login"}, {"path": "//evil.local"}]},
     "references_candidates"),
])
def test_inventory_observations_do_not_create_authorized_targets(tool, output, fact) -> None:
    registry = default_result_mapper_registry()
    mutation = registry.map_result(
        tool_id=ToolId(f"recon.{tool}"), target=HostTarget(host="a.local"), arguments={},
        result=_result(tool_id=f"recon.{tool}", output=output),
    )
    assert len(mutation.nodes) == 1
    node = mutation.nodes[0]
    assert node.node_type is GraphNodeType.HOST
    assert len(node.facts[fact]) == 1
    assert "in_scope" not in node.attrs
    assert "reachable" not in node.attrs
    assert mutation.edges == []


def test_port_scan_only_open_tcp_ports_become_services_without_guessing_http() -> None:
    registry = default_result_mapper_registry()
    output = {"engine_exit_success": True, "host": "a.local", "ports": [
        {"port": 443, "protocol": "tcp", "state": "open"},
        {"port": 80, "protocol": "tcp", "state": "closed"},
        {"port": True, "protocol": "tcp", "state": "open"},
    ]}
    mutation = registry.map_result(
        tool_id=ToolId("recon.port_scan"), target=HostTarget(host="a.local"), arguments={},
        result=_result(tool_id="recon.port_scan", output=output),
    )
    services = [n for n in mutation.nodes if n.node_type is GraphNodeType.SERVICE]
    assert len(services) == 1
    assert services[0].facts == {"host": "a.local", "port": 443, "protocol": "tcp"}
    assert len(mutation.edges) == 1
    output["engine_exit_success"] = False
    assert registry.map_result(
        tool_id=ToolId("recon.port_scan"), target=HostTarget(host="a.local"), arguments={},
        result=_result(tool_id="recon.port_scan", output=output),
    ) == GraphMutation()


@pytest.mark.parametrize(
    "tool", ["dns_resolve", "openapi_inventory", "html_inventory", "port_scan"]
)
@pytest.mark.parametrize("output", [None, "bad", {}, {"addresses": "127.0.0.1"},
                                    {"operations": [None]}, {"references": [42]}])
def test_incomplete_recon_results_do_not_create_graph_nodes(tool, output) -> None:
    assert default_result_mapper_registry().map_result(
        tool_id=ToolId(f"recon.{tool}"), target=HostTarget(host="a.local"), arguments={},
        result=_result(tool_id=f"recon.{tool}", output=output),
    ) == GraphMutation()
