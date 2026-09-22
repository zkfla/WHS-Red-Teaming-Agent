"""``ResultMapperRegistry`` and ``map_recon_http_probe`` in isolation --
no graph, no orchestration state, just the pure mapping function.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from redteam.core.contracts.target import EndpointTarget, HostTarget
from redteam.core.contracts.tool import ToolError, ToolResult
from redteam.core.enums import GraphEdgeType, GraphNodeType
from redteam.core.ids import ActionId, GraphNodeId, InvocationId, ToolId
from redteam.orchestration.observations import (
    EdgeMutation,
    GraphMutation,
    NodeMutation,
    ResultMapperRegistry,
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
    "session_ref": None,
    "status_code": 200,
    "body_bytes": 0,
    "body_sha256": "a" * 64,
    "redirect": False,
    "security_headers_present": {"strict-transport-security": False},
    "body_omitted": True,
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


def test_probe_preserves_observed_https_port_and_original_path() -> None:
    mutation = map_recon_http_probe(
        target=HostTarget(host="a.local"),
        arguments={"target_ref": "x", "path": "/accounts/1234"},
        result=_result(
            output={
                **_GOOD_OUTPUT,
                "host": "a.local",
                "port": 8443,
                "scheme": "https",
            }
        ),
    )

    service = next(node for node in mutation.nodes if node.node_type is GraphNodeType.SERVICE)
    endpoint = next(node for node in mutation.nodes if node.node_type is GraphNodeType.ENDPOINT)
    assert service.facts["port"] == 8443
    assert service.facts["scheme"] == "https"
    assert endpoint.facts["port"] == 8443
    assert endpoint.facts["scheme"] == "https"
    assert endpoint.facts["observed_target"] == "/accounts/1234"
    assert ":8443" in str(endpoint.node_id)
    assert str(endpoint.node_id).endswith("/accounts/{id}")


def test_probe_rejects_a_result_for_a_different_target_ref() -> None:
    assert map_recon_http_probe(
        target=HostTarget(host="a.local"),
        arguments={"target_ref": "expected", "path": "/"},
        result=_result(output=_GOOD_OUTPUT),
    ) == GraphMutation()


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


def test_registry_rejects_an_entire_secret_bearing_mutation_before_update() -> None:
    def _unsafe(*, target, arguments, result):  # noqa: ANN001
        return GraphMutation(
            nodes=[
                NodeMutation(
                    GraphNodeType.HOST,
                    GraphNodeId("host:a.local"),
                    facts={"metadata": {"password": "must-not-be-stored"}},
                )
            ]
        )

    registry = ResultMapperRegistry()
    registry.register("recon.unsafe", _unsafe)

    mutation = registry.map_result(
        tool_id=ToolId("recon.unsafe"),
        target=HostTarget(host="a.local"),
        arguments={},
        result=_result(tool_id="recon.unsafe", output={}),
    )

    assert mutation == GraphMutation()
    assert registry.last_failure is not None
    assert registry.last_failure.reason == "SensitiveValueError"


def test_registry_rejects_edges_to_nodes_the_mapper_did_not_supply() -> None:
    def _dangling(*, target, arguments, result):  # noqa: ANN001
        return GraphMutation(
            edges=[
                EdgeMutation(
                    GraphEdgeType.OBSERVED,
                    GraphNodeId("host:a.local"),
                    GraphNodeId("endpoint:a.local GET /missing"),
                )
            ]
        )

    registry = ResultMapperRegistry()
    registry.register("recon.dangling", _dangling)

    mutation = registry.map_result(
        tool_id=ToolId("recon.dangling"),
        target=HostTarget(host="a.local"),
        arguments={},
        result=_result(tool_id="recon.dangling", output={}),
    )

    assert mutation == GraphMutation()
    assert registry.last_failure is not None
    assert registry.last_failure.reason == "ValueError"


def test_web_mapper_uses_executed_method_path_and_port() -> None:
    from redteam.orchestration.observations.web_http import map_web_http_request

    mutation = map_web_http_request(
        target=EndpointTarget(host='a.local', port=8443, path='/old'),
        arguments={'target_ref': 'x', 'path': '/actual', 'method': 'POST'},
        result=_result(tool_id='web.http_request', output=_GOOD_OUTPUT),
    )
    node = mutation.nodes[0]
    assert str(node.node_id) == 'endpoint:a.local:8443 POST /actual'
    assert node.facts['observed_target'] == '/actual'


def test_web_mapper_models_a_session_reference_and_observed_access() -> None:
    from redteam.orchestration.observations.web_http import map_web_http_request

    mutation = map_web_http_request(
        target=EndpointTarget(host="a.local", path="/private"),
        arguments={
            "target_ref": "x",
            "path": "/private",
            "method": "GET",
            "session_ref": "alice",
        },
        result=_result(
            tool_id="web.http_request",
            output={**_GOOD_OUTPUT, "session_ref": "alice"},
        ),
    )

    assert {node.node_type for node in mutation.nodes} == {
        GraphNodeType.ENDPOINT,
        GraphNodeType.PRINCIPAL,
        GraphNodeType.CREDENTIAL,
    }
    assert {edge.edge_type for edge in mutation.edges} == {
        GraphEdgeType.RELATED_TO,
        GraphEdgeType.CAN_ACCESS,
    }


def test_web_mapper_preserves_only_normalized_response_metadata() -> None:
    from redteam.orchestration.observations.web_http import map_web_http_request

    mutation = map_web_http_request(
        target=EndpointTarget(host="a.local", path="/private"),
        arguments={"target_ref": "x", "path": "/private", "method": "GET"},
        result=_result(
            tool_id="web.http_request",
            output={
                **_GOOD_OUTPUT,
                "redirect_metadata": {
                    "detected": True,
                    "location_path": "/login",
                    "query_omitted": True,
                    "fragment_omitted": False,
                },
                "auth_challenge": {
                    "present": True,
                    "scheme": "basic",
                    "realm_present": True,
                },
                "cookie_metadata": {
                    "present": True,
                    "cookies": [
                        {
                            "name": "sid",
                            "secure": True,
                            "http_only": True,
                            "same_site": "lax",
                            "domain_present": False,
                            "path_present": True,
                            "persistent": False,
                            "value": "must-not-be-copied",
                        }
                    ],
                    "values_omitted": True,
                },
            },
        ),
    )

    endpoint = next(node for node in mutation.nodes if node.node_type is GraphNodeType.ENDPOINT)
    assert endpoint.facts["redirect_metadata"]["location_path"] == "/login"
    assert endpoint.facts["auth_challenge"] == {
        "present": True,
        "scheme": "basic",
        "realm_present": True,
    }
    assert endpoint.facts["cookie_metadata"]["cookies"][0]["name"] == "sid"
    # Assert the intent directly instead of substring-matching the rendered
    # dict: the mapper's own "values_omitted": True flag legitimately contains
    # the characters "value", so a naive containment check fails on a payload
    # that is in fact safe. These two are strictly stronger -- the raw value
    # string must be absent, and no cookie entry may carry a value key at all.
    cookie_metadata = endpoint.facts["cookie_metadata"]
    assert "must-not-be-copied" not in str(cookie_metadata)
    assert cookie_metadata["values_omitted"] is True
    assert cookie_metadata["cookies"]
    for entry in cookie_metadata["cookies"]:
        assert "value" not in entry
        assert "values" not in entry


@pytest.mark.parametrize('status', [True, 0, 600, '200', None])
def test_web_mapper_rejects_invalid_status(status) -> None:
    from redteam.orchestration.observations.web_http import map_web_http_request

    assert map_web_http_request(
        target=EndpointTarget(host='a.local', path='/'),
        arguments={'target_ref': 'x', 'path': '/'},
        result=_result(output={**_GOOD_OUTPUT, 'status_code': status}),
    ) == GraphMutation()
