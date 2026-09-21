"""H1: Recon recovers a SERVICE-only graph, and only on observed evidence.

The negative cases carry most of the weight here. Every path this provider
sends must either be the existing http_probe default or a path the graph
already observed; a test that only checks the happy path would not notice a
provider that quietly started probing /openapi.json "because it usually
exists".
"""

from __future__ import annotations

from redteam.attack_graph.identity import endpoint_node_id, host_node_id, service_node_id
from redteam.attack_graph.service import AttackGraphService
from redteam.attack_graph.stores.memory import InMemoryAttackGraphStore
from redteam.core.enums import GraphNodeType
from redteam.orchestration.agents.providers import GraphCandidateContext
from redteam.orchestration.agents.recon import (
    RECON_HTML_INVENTORY,
    RECON_HTTP_PROBE,
    RECON_OIDC_INVENTORY,
    RECON_OPENAPI_INVENTORY,
    RECON_TLS_INSPECT,
    ReconCandidateProvider,
)
from redteam.orchestration.agents.registry import RECON_AGENT

HOST = "a.local"
TARGET_REF = "lab"


async def graph(
    *,
    host_facts=None,
    service=None,
    endpoints=(),
    extra_nodes=(),
):
    """A graph shaped exactly as the test needs, with no implied defaults."""

    service_graph = AttackGraphService(InMemoryAttackGraphStore())
    await service_graph.upsert_node(
        GraphNodeType.HOST,
        node_id=host_node_id(HOST),
        attrs={"in_scope": True},
        facts={"target_ref": TARGET_REF, **(host_facts or {})},
    )
    if service is not None:
        name, facts, attrs = service
        await service_graph.upsert_node(
            GraphNodeType.SERVICE,
            node_id=service_node_id(name, host=HOST),
            attrs=attrs,
            facts={"host": HOST, **facts},
        )
    for path, facts, attrs in endpoints:
        await service_graph.upsert_node(
            GraphNodeType.ENDPOINT,
            node_id=endpoint_node_id("GET", path, host=HOST),
            attrs=attrs,
            facts={"host": HOST, "path": path, "method": "GET", **facts},
        )
    for node_type, node_id, facts, attrs in extra_nodes:
        await service_graph.upsert_node(
            node_type, node_id=node_id, facts=facts, attrs=attrs
        )
    return service_graph


async def propose(service_graph, *, operations=(), verified=False):
    view = service_graph.view()
    observation = await service_graph.observe(step=1)
    context = GraphCandidateContext(
        view=view,
        observation=observation,
        step=1,
        available_operations=tuple(operations),
        operations_verified=verified,
    )
    return await ReconCandidateProvider().propose_graph(RECON_AGENT, context)


def by_prefix(batch, prefix: str):
    return [item for item in batch.candidates if item.plan_step.step_id.startswith(prefix)]


HTTP_SERVICE = ("http", {"application_protocol": "http"}, {"responded": True})

#: The state the bootstrap exists for: the playbook's host probe has already
#: been attempted on this host (so it is retired and will never run again),
#: yet a service is known and no endpoint was ever produced.
PROBE_RETIRED = {"attempted_operations": ["recon.service_discovery"]}


# -- bootstrap -------------------------------------------------------------


async def test_a_service_without_any_endpoint_is_recovered_by_the_existing_probe() -> None:
    service_graph = await graph(service=HTTP_SERVICE, host_facts=PROBE_RETIRED)

    batch = await propose(service_graph)

    bootstraps = by_prefix(batch, "bootstrap-service:")
    assert len(bootstraps) == 1
    step = bootstraps[0].plan_step
    assert step.tool_id == RECON_HTTP_PROBE
    assert step.capability == "recon"
    assert step.operation == "service_discovery"
    assert step.arguments == {"target_ref": TARGET_REF, "path": "/"}
    assert str(bootstraps[0].target_node_ids[0]) == str(host_node_id(HOST))


async def test_the_bootstrap_is_not_offered_once_an_endpoint_exists() -> None:
    service_graph = await graph(
        service=HTTP_SERVICE,
        endpoints=[("/", {}, {"status_code": 200})],
    )

    batch = await propose(service_graph)

    assert not by_prefix(batch, "bootstrap-service:")


async def test_a_service_without_its_host_is_left_alone() -> None:
    service_graph = AttackGraphService(InMemoryAttackGraphStore())
    await service_graph.upsert_node(
        GraphNodeType.SERVICE,
        node_id=service_node_id("http", host=HOST),
        attrs={"responded": True},
        facts={"host": HOST, "application_protocol": "http", "target_ref": TARGET_REF},
    )

    batch = await propose(service_graph)

    assert not by_prefix(batch, "bootstrap-service:")


async def test_a_non_http_service_is_not_probed() -> None:
    service_graph = await graph(
        service=("tcp:22", {"port": 22, "transport_protocol": "tcp"}, {"open": True}),
        host_facts=PROBE_RETIRED,
    )

    batch = await propose(service_graph)

    assert not by_prefix(batch, "bootstrap-service:")


async def test_an_http_port_alone_is_not_an_observed_http_service() -> None:
    # A port number is an assignment, not a measurement. Nothing observed says
    # the listener speaks HTTP, so the bootstrap must not fire on the port.
    service_graph = await graph(
        service=("tcp:443", {"port": 443, "transport_protocol": "tcp"}, {"open": True}),
        host_facts=PROBE_RETIRED,
    )

    batch = await propose(service_graph)

    assert not by_prefix(batch, "bootstrap-service:")


async def test_a_tls_service_alone_is_not_an_observed_http_service() -> None:
    service_graph = await graph(
        service=("tls", {"service_name": "tls"}, {"tls_verified": True}),
        host_facts=PROBE_RETIRED,
    )

    batch = await propose(service_graph)

    assert not by_prefix(batch, "bootstrap-service:")


async def test_no_target_ref_means_no_bootstrap() -> None:
    service_graph = AttackGraphService(InMemoryAttackGraphStore())
    await service_graph.upsert_node(
        GraphNodeType.HOST,
        node_id=host_node_id(HOST),
        attrs={"in_scope": True},
        facts=PROBE_RETIRED,
    )
    await service_graph.upsert_node(
        GraphNodeType.SERVICE,
        node_id=service_node_id("http", host=HOST),
        attrs={"responded": True},
        facts={"host": HOST, "application_protocol": "http"},
    )

    batch = await propose(service_graph)

    assert not by_prefix(batch, "bootstrap-service:")


async def test_a_catalog_that_cannot_execute_the_probe_yields_no_candidate() -> None:
    service_graph = await graph(service=HTTP_SERVICE, host_facts=PROBE_RETIRED)

    batch = await propose(service_graph, operations=(), verified=True)

    assert not by_prefix(batch, "bootstrap-service:")
    assert [item.reason for item in batch.diagnostics] == [
        "no_executable_service_discovery_tool"
    ]


async def test_the_bootstrap_does_not_duplicate_a_playbook_probe() -> None:
    # The playbook already offers a probe for this in-scope host this turn;
    # adding a second one would be duplicate work rather than recovery.
    service_graph = await graph(service=HTTP_SERVICE)

    batch = await propose(service_graph)

    service_discovery = [
        item
        for item in batch.candidates
        if item.plan_step.operation == "service_discovery"
    ]
    assert len(service_discovery) == 1
    assert service_discovery[0].plan_step.step_id == "probe-host"


async def test_the_bootstrap_takes_over_once_the_playbook_probe_is_retired() -> None:
    # Same graph as above, only the host probe has already been attempted --
    # which is exactly the state that otherwise stalls forever.
    service_graph = await graph(service=HTTP_SERVICE, host_facts=PROBE_RETIRED)

    batch = await propose(service_graph)

    assert by_prefix(batch, "bootstrap-service:")


# -- inventory -------------------------------------------------------------


async def test_a_successful_endpoint_gets_an_html_inventory() -> None:
    service_graph = await graph(
        service=HTTP_SERVICE,
        endpoints=[("/home", {"target_ref": TARGET_REF}, {"status_code": 200})],
    )

    batch = await propose(service_graph)

    drafts = by_prefix(batch, "html-inventory:")
    assert len(drafts) == 1
    assert drafts[0].plan_step.tool_id == RECON_HTML_INVENTORY
    assert drafts[0].plan_step.arguments == {"target_ref": TARGET_REF, "path": "/home"}
    assert drafts[0].plan_step.assessment_kind == "observation"


async def test_an_endpoint_that_never_answered_gets_no_inventory() -> None:
    service_graph = await graph(
        service=HTTP_SERVICE,
        endpoints=[("/home", {"target_ref": TARGET_REF}, {})],
    )

    batch = await propose(
        service_graph, operations=(("recon", "endpoint_discovery"),), verified=True
    )

    inventory_steps = [
        item.plan_step.tool_id
        for item in batch.candidates
        if "inventory" in item.plan_step.step_id
    ]
    assert inventory_steps == []
    # The reason it did nothing is recorded rather than silently dropped.
    assert "endpoint_never_responded" in [item.reason for item in batch.diagnostics]


async def test_a_non_success_status_gets_no_html_inventory() -> None:
    service_graph = await graph(
        service=HTTP_SERVICE,
        endpoints=[("/private", {"target_ref": TARGET_REF}, {"status_code": 401})],
    )

    batch = await propose(
        service_graph,
        operations=(("recon", "endpoint_discovery"),),
        verified=True,
    )

    assert not by_prefix(batch, "html-inventory:")


async def test_an_observed_openapi_path_gets_the_openapi_inventory() -> None:
    service_graph = await graph(
        service=HTTP_SERVICE,
        endpoints=[("/api/openapi.json", {"target_ref": TARGET_REF}, {"status_code": 200})],
    )

    batch = await propose(
        service_graph, operations=(("recon", "endpoint_discovery"),), verified=True
    )

    drafts = by_prefix(batch, "openapi-inventory:")
    assert len(drafts) == 1
    assert drafts[0].plan_step.tool_id == RECON_OPENAPI_INVENTORY
    assert drafts[0].plan_step.arguments["path"] == "/api/openapi.json"


async def test_an_observed_oidc_path_gets_the_oidc_inventory() -> None:
    service_graph = await graph(
        service=HTTP_SERVICE,
        endpoints=[
            (
                "/.well-known/openid-configuration",
                {"target_ref": TARGET_REF},
                {"status_code": 200},
            )
        ],
    )

    batch = await propose(
        service_graph, operations=(("recon", "endpoint_discovery"),), verified=True
    )

    drafts = by_prefix(batch, "oidc-inventory:")
    assert len(drafts) == 1
    assert drafts[0].plan_step.tool_id == RECON_OIDC_INVENTORY


async def test_a_standard_document_path_is_never_invented() -> None:
    # The endpoint responded, but nothing in the graph names an API document.
    # Probing the conventional locations would be a guess, so nothing is
    # offered for them.
    service_graph = await graph(
        service=HTTP_SERVICE,
        endpoints=[("/", {"target_ref": TARGET_REF}, {"status_code": 200})],
    )

    batch = await propose(
        service_graph, operations=(("recon", "endpoint_discovery"),), verified=True
    )

    assert not by_prefix(batch, "openapi-inventory:")
    assert not by_prefix(batch, "oidc-inventory:")
    for draft in batch.candidates:
        for marker in ("openapi", "swagger", "well-known", "oidc"):
            assert marker not in str(draft.plan_step.arguments.get("path", "")).lower()


# -- TLS -------------------------------------------------------------------


async def test_an_observed_https_service_justifies_a_tls_inspection() -> None:
    service_graph = await graph(
        service=("https", {"application_protocol": "https"}, {"responded": True}),
    )

    batch = await propose(service_graph)

    drafts = by_prefix(batch, "tls-inspect:")
    assert len(drafts) == 1
    assert drafts[0].plan_step.tool_id == RECON_TLS_INSPECT
    assert drafts[0].plan_step.arguments == {"target_ref": TARGET_REF}


async def test_a_scheme_fact_is_also_enough_for_a_tls_inspection() -> None:
    service_graph = await graph(
        service=("https", {"scheme": "https"}, {"responded": True}),
    )

    batch = await propose(service_graph)

    assert by_prefix(batch, "tls-inspect:")


async def test_an_already_verified_tls_service_is_not_inspected_again() -> None:
    service_graph = await graph(
        service=(
            "https",
            {"application_protocol": "https"},
            {"responded": True, "tls_verified": True},
        ),
    )

    batch = await propose(service_graph)

    assert not by_prefix(batch, "tls-inspect:")


async def test_without_an_observed_tls_fact_no_tls_inspection_is_offered() -> None:
    service_graph = await graph(
        service=HTTP_SERVICE,
        endpoints=[("/", {"target_ref": TARGET_REF}, {"status_code": 200})],
    )

    batch = await propose(service_graph)

    assert not by_prefix(batch, "tls-inspect:")


async def test_an_observed_hsts_header_alone_does_not_justify_a_tls_inspection() -> None:
    # HSTS only *hints* at TLS. The plan gates this candidate on SERVICE facts
    # that actually show HTTPS/TLS, and a hint is not a transport fact.
    service_graph = await graph(
        service=HTTP_SERVICE,
        endpoints=[
            (
                "/",
                {
                    "target_ref": TARGET_REF,
                    "security_headers_present": {"strict-transport-security": True},
                },
                {"status_code": 200},
            )
        ],
    )

    batch = await propose(service_graph)

    assert not by_prefix(batch, "tls-inspect:")
