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
from redteam.orchestration.observations import GraphMutation, ResultMapperRegistry
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


def test_web_mapper_uses_executed_method_path_and_port() -> None:
    from redteam.orchestration.observations.web_http import map_web_http_request

    mutation = map_web_http_request(
        target=EndpointTarget(host='a.local', port=8443, path='/old'),
        arguments={'path': '/actual', 'method': 'POST'},
        result=_result(tool_id='web.http_request', output=_GOOD_OUTPUT),
    )
    node = mutation.nodes[0]
    assert str(node.node_id) == 'endpoint:a.local:8443 POST /actual'
    assert node.facts['observed_target'] == '/actual'


@pytest.mark.parametrize('status', [True, 0, 600, '200', None])
def test_web_mapper_rejects_invalid_status(status) -> None:
    from redteam.orchestration.observations.web_http import map_web_http_request

    assert map_web_http_request(
        target=EndpointTarget(host='a.local', path='/'), arguments={'path': '/'},
        result=_result(output={'status_code': status}),
    ) == GraphMutation()
