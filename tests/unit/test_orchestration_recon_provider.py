"""H1: Recon recovers a SERVICE-only graph, and only on observed evidence.

The negative cases carry most of the weight here. Every path this provider
sends must either be the existing http_probe default or a path the graph
already observed; a test that only checks the happy path would not notice a
provider that quietly started probing /openapi.json "because it usually
exists".
"""

from __future__ import annotations

from datetime import datetime, timezone

from redteam.attack_graph.identity import (
    endpoint_node_id,
    host_node_id,
    node_id_for_target,
    service_node_id,
)
from redteam.attack_graph.service import AttackGraphService
from redteam.attack_graph.stores.memory import InMemoryAttackGraphStore
from redteam.capability.gateway import MCPToolGateway
from redteam.capability.registry import MCPToolRegistry
from redteam.core.contracts.target import HostTarget
from redteam.core.contracts.tool import ToolDescriptor, ToolResult
from redteam.core.enums import GraphNodeType, RiskLevel, ToolHealthStatus
from redteam.core.ids import ActionId, InvocationId, ToolId
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
from redteam.orchestration.candidates.compiler import CandidateCompiler
from redteam.orchestration.observations import default_result_mapper_registry
from tests.unit.test_result_mapper_inventory import _html_output, _openapi_output

HOST = "a.local"
TARGET_REF = "lab"

#: Each recon tool bound to the canonical operation its candidate declares.
_RECON_TOOLS = (
    ("recon.http_probe", "service_discovery"),
    ("recon.html_inventory", "endpoint_discovery"),
    ("recon.openapi_inventory", "endpoint_discovery"),
    ("recon.oidc_inventory", "endpoint_discovery"),
    ("recon.tls_inspect", "service_discovery"),
)


def recon_catalog() -> MCPToolGateway:
    """A real, operation-aware ToolCatalog over the recon tools."""

    registry = MCPToolRegistry()
    for tool_id, _operation in _RECON_TOOLS:
        registry.register(
            ToolDescriptor(
                tool_id=ToolId(tool_id),
                server_id="test",
                tool_name=tool_id.split(".")[1],
                input_schema={
                    "type": "object",
                    "properties": {
                        "target_ref": {"type": "string"},
                        "path": {"type": "string"},
                    },
                    "required": ["target_ref"],
                },
                capability_tags=["recon"],
                risk_level=RiskLevel.LOW,
                health_status=ToolHealthStatus.HEALTHY,
            )
        )
    return MCPToolGateway(
        registry, {}, {tool_id: [operation] for tool_id, operation in _RECON_TOOLS}
    )


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


async def test_inventory_does_not_visit_an_explicitly_unallowlisted_path() -> None:
    service_graph = await graph(
        service=HTTP_SERVICE,
        endpoints=[(
            "/assets/test-sdk.js",
            {"target_ref": TARGET_REF, "path_allowlisted": False},
            {"status_code": 200, "path_allowlisted": False},
        )],
    )

    batch = await propose(service_graph)

    assert not [
        item for item in batch.candidates
        if item.plan_step.arguments.get("path") == "/assets/test-sdk.js"
    ]


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
    # Execution target and graph target are the same node; the SERVICE that
    # justified the candidate is carried as provenance instead.
    assert [str(item) for item in drafts[0].target_node_ids] == [str(host_node_id(HOST))]
    hypothesis = drafts[0].plan_step.hypothesis
    assert hypothesis is not None
    assert hypothesis.metadata["source_node_id"] == str(
        service_node_id("https", host=HOST)
    )


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


# -- every generated draft must survive the compiler -----------------------


def _oidc_output() -> dict:
    """The real recon.oidc_inventory output shape."""

    return {
        "capabilities": {
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["client_secret_basic"],
        },
        "endpoints_present": {
            "authorization_endpoint": True,
            "token_endpoint": True,
            "jwks_uri": True,
            "revocation_endpoint": False,
        },
        "source_untrusted": True,
        "endpoints_followed": False,
        "verdict": "observation_only",
    }


def _tool_result(tool_id: str, output: dict) -> ToolResult:
    return ToolResult(
        invocation_id=InvocationId("inv_inventory"),
        action_id=ActionId("action_inventory"),
        tool_id=ToolId(tool_id),
        success=True,
        # The fixture's own target_ref must not win: the Mapper requires the
        # output to agree with the argument actually sent.
        output={**output, "target_ref": TARGET_REF},
        started_at=datetime.now(timezone.utc),
        finished_at=datetime.now(timezone.utc),
    )


async def test_every_inventory_draft_actually_maps_a_successful_result() -> None:
    # The second half of the same class of bug: a draft that compiles but
    # whose Mapper silently returns nothing for the target kind it carries is
    # still a dead end. html/openapi map through _common_context (host/port
    # off any target), while oidc_inventory maps through _host_context, which
    # requires a HostTarget -- verified directly, which is why OIDC is the one
    # inventory candidate executed against the HOST.
    cases = (
        (RECON_HTML_INVENTORY, "/home", _html_output()),
        (RECON_OPENAPI_INVENTORY, "/api/openapi.json", _openapi_output()),
        (RECON_OIDC_INVENTORY, "/.well-known/openid-configuration", _oidc_output()),
    )

    for tool_id, path, output in cases:
        assert path.startswith("/")
        service_graph = await graph(
            service=HTTP_SERVICE,
            endpoints=[(path, {"target_ref": TARGET_REF}, {"status_code": 200})],
            host_facts=PROBE_RETIRED,
        )
        batch = await propose(service_graph)
        drafts = [item for item in batch.candidates if item.plan_step.tool_id == tool_id]
        assert len(drafts) == 1, tool_id

        compiled, rejections = CandidateCompiler(
            recon_catalog()
        ).compile_drafts_with_rejections(drafts)
        assert [item.reason for item in rejections] == [], tool_id
        candidate = compiled[0]

        mutation = default_result_mapper_registry().map_result(
            tool_id=ToolId(tool_id),
            target=candidate.plan_step.resolved_target(),
            arguments=candidate.plan_step.arguments,
            result=_tool_result(tool_id, output),
        )

        assert mutation.nodes, f"{tool_id} compiled but mapped to nothing"
        # Each of these discovers endpoints; openapi additionally materializes
        # a RESOURCE template, so this is "at least one endpoint", not "only".
        assert any(
            node.node_type is GraphNodeType.ENDPOINT for node in mutation.nodes
        ), tool_id


def test_the_tls_mapper_only_maps_a_host_scoped_target() -> None:
    # Verified against the real Mapper, because it is the reason this
    # candidate cannot be ServiceTarget-scoped: recon.tls_inspect handshakes
    # the target's *origin* and the Mapper requires a HostTarget, silently
    # returning nothing for anything else. A ServiceTarget would compile fine
    # and then never map a result -- a dead end, not an alignment fix.
    from redteam.core.contracts.target import ServiceTarget
    from redteam.core.contracts.tool import ToolResult
    from redteam.core.ids import InvocationId
    from redteam.orchestration.observations.recon_network import map_recon_tls_inspect

    output = {
        "target_ref": TARGET_REF,
        "verified": True,
        "protocol": "TLSv1.3",
        "cipher": "TLS_AES_256_GCM_SHA384",
        "certificate_sha256": "a" * 64,
    }
    # The handshake publishes the result under the host's "tls" SERVICE, so
    # the SERVICE node is an output of this tool, not its execution target.
    result = ToolResult(
        invocation_id=InvocationId("inv_tls"),
        action_id=ActionId("action_tls"),
        tool_id=ToolId("recon.tls_inspect"),
        success=True,
        output=output,
        started_at=datetime.now(timezone.utc),
        finished_at=datetime.now(timezone.utc),
    )
    arguments = {"target_ref": TARGET_REF}

    host_scoped = map_recon_tls_inspect(
        target=HostTarget(host=HOST), arguments=arguments, result=result
    )
    service_scoped = map_recon_tls_inspect(
        target=ServiceTarget(service_name="tls", host=HOST),
        arguments=arguments,
        result=result,
    )

    assert [node.node_type for node in host_scoped.nodes] == [GraphNodeType.SERVICE]
    assert str(host_scoped.nodes[0].node_id) == str(service_node_id("tls", host=HOST))
    observation = host_scoped.nodes[0].observations[0]
    assert observation["kind"] == "tls_handshake"
    assert observation["verified"] is True
    # Nothing at all for a service-scoped target: the result is dropped.
    assert service_scoped.nodes == []


async def test_generated_inventory_and_tls_drafts_compile_cleanly() -> None:
    # The regression this guards: these drafts used to name an ENDPOINT or
    # SERVICE node in target_node_ids while carrying a HostTarget execution
    # target, so the compiler correctly refused every one of them as
    # target_mismatch and nothing was ever executable.
    service_graph = await graph(
        service=("https", {"application_protocol": "https"}, {"responded": True}),
        endpoints=[
            ("/home", {"target_ref": TARGET_REF}, {"status_code": 200}),
            ("/api/openapi.json", {"target_ref": TARGET_REF}, {"status_code": 200}),
            (
                "/.well-known/openid-configuration",
                {"target_ref": TARGET_REF},
                {"status_code": 200},
            ),
        ],
        host_facts=PROBE_RETIRED,
    )
    batch = await propose(service_graph)

    compiled, rejections = CandidateCompiler(
        recon_catalog()
    ).compile_drafts_with_rejections(batch.candidates)

    assert [item.reason for item in rejections if item.reason == "target_mismatch"] == []
    assert {
        RECON_HTML_INVENTORY,
        RECON_OPENAPI_INVENTORY,
        RECON_OIDC_INVENTORY,
        RECON_TLS_INSPECT,
    } <= {item.plan_step.tool_id for item in compiled}
    # And the invariant itself: whatever executes must be a node the draft
    # actually named.
    for item in compiled:
        target_node = node_id_for_target(item.plan_step.resolved_target())
        assert target_node in item.draft.target_node_ids


async def test_the_bootstrap_draft_compiles_cleanly() -> None:
    service_graph = await graph(service=HTTP_SERVICE, host_facts=PROBE_RETIRED)
    batch = await propose(service_graph)

    compiled, rejections = CandidateCompiler(
        recon_catalog()
    ).compile_drafts_with_rejections(batch.candidates)

    assert [item.reason for item in rejections] == []
    assert {item.plan_step.tool_id for item in compiled} >= {RECON_HTTP_PROBE}


async def test_an_inventory_draft_pointing_at_the_host_is_still_rejected() -> None:
    # The compiler invariant is unchanged and must stay enforced: an
    # EndpointTarget's own node is what an inventory option executes against,
    # so substituting a HOST target is a real mismatch, not a technicality.
    service_graph = await graph(
        service=HTTP_SERVICE,
        endpoints=[("/home", {"target_ref": TARGET_REF}, {"status_code": 200})],
        host_facts=PROBE_RETIRED,
    )
    batch = await propose(service_graph)
    mismatched = [
        draft.model_copy(
            update={
                "plan_step": draft.plan_step.model_copy(
                    update={"target": HostTarget(host=HOST)}
                )
            }
        )
        for draft in batch.candidates
        if draft.plan_step.tool_id == RECON_HTML_INVENTORY
    ]

    assert mismatched, "the html inventory draft must exist for this graph"
    _, rejections = CandidateCompiler(
        recon_catalog()
    ).compile_drafts_with_rejections(mismatched)

    assert [item.reason for item in rejections] == ["target_mismatch"]


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
