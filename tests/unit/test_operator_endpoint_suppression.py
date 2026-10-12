"""Operator endpoint exclusion is enforced at both executable frontiers."""


from redteam.attack_graph.identity import endpoint_node_id, host_node_id
from redteam.orchestration.operator_intents.suppression import endpoint_excluded


def test_path_exclusion_is_exact_and_url_exclusion_is_authority_bound() -> None:
    endpoint = endpoint_node_id("GET", "/api/docs", host="a.local")
    assert endpoint_excluded(endpoint, ["/api/docs"])
    assert endpoint_excluded(endpoint, ["https://a.local/api/docs"])
    assert not endpoint_excluded(endpoint, ["/api/doc"])
    assert not endpoint_excluded(endpoint, ["/api/docs-old"])
    assert not endpoint_excluded(endpoint, ["https://b.local/api/docs"])
    assert not endpoint_excluded(host_node_id("a.local"), ["/api/docs"])
    assert endpoint_excluded(
        endpoint_node_id("GET", "/orders/1234", host="a.local"), ["/orders/1234"]
    )
