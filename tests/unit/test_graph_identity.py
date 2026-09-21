"""Attack Graph identity: stable ids and TargetRef <-> node round trips."""

from __future__ import annotations

import pytest

from redteam.attack_graph.identity import (
    InvalidNodeIdError,
    endpoint_node_id,
    host_node_id,
    is_well_formed,
    make_node_id,
    node_id_for_target,
    node_type_for_target,
    normalize_path,
    path_parameters,
    split_node_id,
    target_for_node,
)
from redteam.core.contracts.target import (
    AssetTarget,
    DomainTarget,
    EndpointTarget,
    HostTarget,
    PrincipalTarget,
    ServiceTarget,
)
from redteam.core.enums import GraphNodeType
from redteam.core.ids import AssetId, GraphNodeId, PrincipalId


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("/orders/8831/items", "/orders/{id}/items"),
        ("/orders/8831?page=2", "/orders/{id}"),
        ("/users/7a3f21b0/profile", "/users/{id}/profile"),
        ("/health", "/health"),
        ("/v1/accounts", "/v1/accounts"),
        ("", "/"),
    ],
)
def test_normalize_path_collapses_identifier_segments(raw: str, expected: str) -> None:
    assert normalize_path(raw) == expected


def test_same_endpoint_with_different_ids_is_one_node() -> None:
    # Without this, every request against an id-bearing URL would mint a
    # new node and the graph would never converge.
    assert endpoint_node_id("get", "/orders/1") == endpoint_node_id("GET", "/orders/999")


def test_host_ids_are_case_insensitive() -> None:
    assert host_node_id("Example.LOCAL") == host_node_id("example.local")


def test_make_and_split_node_id_round_trip() -> None:
    node_id = make_node_id(GraphNodeType.SERVICE, "h1/ssh")
    assert split_node_id(node_id) == (GraphNodeType.SERVICE, "h1/ssh")
    assert is_well_formed(node_id)


@pytest.mark.parametrize("bad", ["example.local", "nope:x", ":x", "host:"])
def test_malformed_node_ids_are_rejected(bad: str) -> None:
    assert not is_well_formed(GraphNodeId(bad))
    with pytest.raises(InvalidNodeIdError):
        split_node_id(GraphNodeId(bad))


def test_make_node_id_rejects_empty_key() -> None:
    with pytest.raises(InvalidNodeIdError):
        make_node_id(GraphNodeType.HOST, "")


def test_explicit_graph_node_id_on_a_target_wins() -> None:
    target = HostTarget(host="a.local", graph_node_id=GraphNodeId("host:pinned"))
    assert node_id_for_target(target) == GraphNodeId("host:pinned")


@pytest.mark.parametrize(
    ("target", "expected_id", "expected_type"),
    [
        (HostTarget(host="a.local"), "host:a.local", GraphNodeType.HOST),
        (DomainTarget(domain="A.Local"), "host:a.local", GraphNodeType.HOST),
        (
            ServiceTarget(service_name="ssh", host="a.local"),
            "service:a.local/ssh",
            GraphNodeType.SERVICE,
        ),
        (
            EndpointTarget(host="a.local", path="/orders/12"),
            "endpoint:a.local ANY /orders/{id}",
            GraphNodeType.ENDPOINT,
        ),
        (
            PrincipalTarget(principal_id=PrincipalId("svc-1")),
            "principal:svc-1",
            GraphNodeType.PRINCIPAL,
        ),
        (AssetTarget(asset_id=AssetId("db-1")), "asset:db-1", GraphNodeType.ASSET),
    ],
)
def test_every_target_kind_resolves_to_a_node(target, expected_id, expected_type) -> None:
    # The skeleton's update node had to assert graph_node_id was set;
    # every kind now derives one, so that gap is closed.
    assert node_id_for_target(target) == GraphNodeId(expected_id)
    assert node_type_for_target(target) is expected_type


def test_target_for_node_produces_a_scope_checkable_target() -> None:
    # A GraphNodeTarget has no host-like identity, so scope would deny it
    # for the wrong reason.
    host = target_for_node(host_node_id("a.local"), GraphNodeType.HOST)
    assert isinstance(host, HostTarget)
    assert host.host == "a.local"
    assert host.graph_node_id == host_node_id("a.local")

    service = target_for_node(
        make_node_id(GraphNodeType.SERVICE, "a.local/ssh"), GraphNodeType.SERVICE
    )
    assert isinstance(service, ServiceTarget)
    assert (service.service_name, service.host) == ("ssh", "a.local")

    endpoint = target_for_node(
        endpoint_node_id("GET", "/orders/1", "a.local"), GraphNodeType.ENDPOINT
    )
    assert isinstance(endpoint, EndpointTarget)
    assert (endpoint.host, endpoint.path) == ("a.local", "/orders/{id}")


def test_target_for_node_round_trips_the_node_id() -> None:
    for node_id, node_type in [
        (host_node_id("a.local"), GraphNodeType.HOST),
        (make_node_id(GraphNodeType.SERVICE, "a.local/ssh"), GraphNodeType.SERVICE),
        (make_node_id(GraphNodeType.PRINCIPAL, "svc-1"), GraphNodeType.PRINCIPAL),
        (make_node_id(GraphNodeType.ASSET, "db-1"), GraphNodeType.ASSET),
    ]:
        assert node_id_for_target(target_for_node(node_id, node_type)) == node_id


def test_path_parameters_names_object_identifier_positions() -> None:
    # #2: the endpoint's Parameters -- the object-identifier positions the
    # path templates over, named after the collection they identify.
    assert path_parameters("/orders/8831") == ["orders_id"]
    assert path_parameters("/orders/8831/items/42") == ["orders_id", "items_id"]
    assert path_parameters("/login") == []
    assert path_parameters("/") == []
