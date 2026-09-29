"""Graph-driven Recon follow-ups: recover a SERVICE, then inventory evidence.

Two jobs, both bounded by facts the graph actually holds.

1. **Bootstrap.** WEB_API_AGENT.inspect-http-service can never run: a
   ServiceTarget has no path, so the step always drops as
   graph_prerequisite_missing(path). A graph that reached
   "HOST + HTTP SERVICE, no ENDPOINT" therefore stalls, even though
   recon.http_probe -- which *does* produce an ENDPOINT through its
   existing Result Mapper -- is already catalog-bound. Recon owns the
   recovery because the tool is a recon tool; Web never reaches around the
   catalog to call it.

2. **Inventory / TLS follow-up.** recon.html_inventory,
   recon.openapi_inventory, recon.oidc_inventory and recon.tls_inspect all
   have Mappers but no candidate producer. Each is offered only from an
   observed fact, never from "such a path usually exists".

What this module deliberately never does: invent a path. The bootstrap path
is the existing http_probe default (a single forward slash), and every
inventory path is a path the graph already observed. /openapi.json,
/swagger.json, /.well-known/openid-configuration and friends are
*recognised* when they appear in the graph -- never fabricated to go
looking for one.

Evidence bars, and the signals deliberately rejected as too strong an
inference:

* **Bootstrap** requires an explicit transport fact on the SERVICE:
  application_protocol is "http" or "https". Port numbers alone are *not*
  accepted. An open TCP port does not prove HTTP -- a port number is an
  assignment that a service may ignore -- so the port_scan signal
  (port in 80/443/8080/...) is rejected even though it would make the
  bootstrap fire far more often.
* **HTML inventory** requires an observed HTTP 200 on that endpoint, not
  merely "some response". recon.html_inventory's own contract rejects
  anything that is not exactly HTTP 200 with a text/html content type, so a
  401/403/3xx observation is a response the tool is already known to
  refuse. Matching the tool's declared precondition against an observed
  status is not an inference; proposing anyway would spend an action on a
  call whose failure is already known. This is narrower than the plan's
  "an observed response" wording, deliberately, in the direction that never
  fabricates.
* **OpenAPI / OIDC inventory** require only that such a path is already in
  the graph, which is the plan's own condition. The path is never invented;
  a conventional location that nobody observed produces no candidate.
* **TLS inspection** requires an explicit transport fact saying HTTPS
  (application_protocol or scheme is "https") and no verified handshake on
  record yet. Observed Strict-Transport-Security is *not* used: it hints at
  TLS, but a hint is not a transport fact, and the plan gates this on
  "SERVICE facts actually showing HTTPS/TLS".
"""

from __future__ import annotations

from typing import Any

from redteam.attack_graph.identity import host_node_id, node_id_for_target
from redteam.attack_graph.observation import GraphObservation
from redteam.attack_graph.predicates import attrs_of, facts_of
from redteam.attack_graph.view import AttackGraphView
from redteam.core.contracts.graph import GraphNode
from redteam.core.contracts.target import AnyTarget, EndpointTarget, HostTarget
from redteam.core.enums import GraphNodeType
from redteam.core.ids import ToolId
from redteam.orchestration.agents.base import AgentSpec, HypothesisDraft, PlanStep
from redteam.orchestration.agents.providers import (
    CandidateBatch,
    CandidateDiagnostic,
    DraftDrop,
    GraphCandidateContext,
    PlaybookCandidateProvider,
    endpoint_path_explicitly_disallowed,
)
from redteam.orchestration.candidates.models import CandidateDraft, PrioritySignals

RECON_HTTP_PROBE = ToolId("recon.http_probe")
RECON_TLS_INSPECT = ToolId("recon.tls_inspect")
RECON_HTML_INVENTORY = ToolId("recon.html_inventory")
RECON_OPENAPI_INVENTORY = ToolId("recon.openapi_inventory")
RECON_OIDC_INVENTORY = ToolId("recon.oidc_inventory")

_SERVICE_DISCOVERY_OPERATION = "service_discovery"
#: The three inventory Mappers label their own observations
#: "endpoint_discovery"; keep the canonical intent identical to that.
_ENDPOINT_DISCOVERY_OPERATION = "endpoint_discovery"

#: The existing http_probe default, and the only path this provider is
#: allowed to use without the graph having observed it. It is not a guess:
#: it is the tool's own declared default and the value the recon playbook
#: already sends for a host probe.
_BOOTSTRAP_PATH = "/"

#: Observed path fragments that identify a served API-description document.
_OPENAPI_PATH_MARKERS = ("openapi", "swagger")
_OIDC_PATH_MARKERS = ("openid-configuration", "oauth-authorization-server")

#: recon.html_inventory requires exactly this status plus a text/html
#: content type; matching it against the observed status is the tightest
#: evidence bar that is still a fact rather than an inference.
_HTML_INVENTORY_STATUS = 200

#: Transport facts that explicitly say HTTPS.
_HTTPS_PROTOCOLS = frozenset({"https"})
_HTTP_PROTOCOLS = frozenset({"http", "https"})


def _http_family_service(facts: dict[str, Any]) -> bool:
    """Whether this SERVICE was observed to be HTTP-family.

    Exactly one signal is accepted: the SERVICE carries an explicit
    application_protocol of "http" or "https", which the http_probe Mapper
    writes from a real response.

    Deliberately *not* used here: an open TCP port. A port number is an
    assignment, not a measurement -- nothing observed says the listener
    actually speaks HTTP -- so using it would be a stronger inference than
    "an HTTP SERVICE was observed".
    """

    protocol = facts.get("application_protocol")
    return isinstance(protocol, str) and protocol.strip().lower() in _HTTP_PROTOCOLS


def _observed_https_service(facts: dict[str, Any], attrs: dict[str, Any]) -> bool:
    """Whether a SERVICE fact explicitly says this transport is HTTPS.

    An already-verified handshake is excluded: there would be nothing left
    to inspect.
    """

    if attrs.get("tls_verified") is True or facts.get("tls_verified") is True:
        return False
    protocol = facts.get("application_protocol")
    if isinstance(protocol, str) and protocol.strip().lower() in _HTTPS_PROTOCOLS:
        return True
    scheme = facts.get("scheme")
    return isinstance(scheme, str) and scheme.strip().lower() in _HTTPS_PROTOCOLS


def _target_ref(*sources: dict[str, Any]) -> str | None:
    for source in sources:
        value = source.get("target_ref")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _observed_statuses(facts: dict[str, Any], attrs: dict[str, Any]) -> list[int]:
    """Every HTTP status this node actually recorded, in any documented slot."""

    found: list[int] = []
    attr_status = attrs.get("status_code")
    if type(attr_status) is int:
        found.append(attr_status)
    for item in facts.get("observations", []) or []:
        if isinstance(item, dict) and type(item.get("status_code")) is int:
            found.append(item["status_code"])
    return found


def _looks_like_api_document(path: str, markers: tuple[str, ...]) -> bool:
    return any(marker in path.lower() for marker in markers)


class ReconCandidateProvider:
    """Playbook drafts plus graph-derived recon follow-ups."""

    declared_operations = (
        ("recon", _SERVICE_DISCOVERY_OPERATION),
        ("recon", _ENDPOINT_DISCOVERY_OPERATION),
    )

    async def propose(
        self,
        spec: AgentSpec,
        view: AttackGraphView,
        observation: GraphObservation,
        *,
        step: int,
    ) -> CandidateBatch:
        return await self._build(spec, view, observation, step=step, context=None)

    async def propose_graph(
        self, spec: AgentSpec, context: GraphCandidateContext
    ) -> CandidateBatch:
        """The authoritative path: plan from the graph, not just the playbook."""

        return await self._build(
            spec,
            context.view,
            context.observation,
            step=context.step,
            context=context,
        )

    async def _build(
        self,
        spec: AgentSpec,
        view: AttackGraphView,
        observation: GraphObservation,
        *,
        step: int,
        context: GraphCandidateContext | None,
    ) -> CandidateBatch:
        playbook = PlaybookCandidateProvider()
        drops: list[DraftDrop] = []
        if context is not None:
            inherited = await playbook.propose_graph(spec, context)
            candidates, drops = list(inherited.candidates), list(inherited.drops)
        else:
            candidates = list(await playbook.propose(spec, view, observation, step=step))

        diagnostics: list[CandidateDiagnostic] = []
        nodes = sorted(await view.list_nodes(), key=lambda item: str(item.id))
        # A host whose probe the playbook is *currently* offering already has
        # a service_discovery candidate this turn; adding a second one would
        # be duplicate work, not recovery.
        covered_hosts = {
            str(draft.target_node_ids[0])
            for draft in candidates
            if draft.plan_step.operation == _SERVICE_DISCOVERY_OPERATION
        }
        endpoints_by_host: dict[str, list[GraphNode]] = {}
        for node in nodes:
            if node.type is not GraphNodeType.ENDPOINT:
                continue
            host = facts_of(node.properties).get("host")
            if isinstance(host, str):
                endpoints_by_host.setdefault(host.strip().lower(), []).append(node)

        for node in nodes:
            if node.type is GraphNodeType.SERVICE:
                bootstrap = await self._bootstrap_draft(
                    spec=spec,
                    view=view,
                    node=node,
                    context=context,
                    covered_hosts=covered_hosts,
                    endpoints_by_host=endpoints_by_host,
                    diagnostics=diagnostics,
                )
                if bootstrap is not None:
                    candidates.append(bootstrap)
                tls = await self._tls_draft(
                    spec=spec, view=view, node=node, context=context
                )
                if tls is not None:
                    candidates.append(tls)
            elif node.type is GraphNodeType.ENDPOINT:
                if endpoint_path_explicitly_disallowed(node):
                    continue
                candidates.extend(
                    await self._inventory_drafts(
                        spec=spec,
                        view=view,
                        node=node,
                        context=context,
                        diagnostics=diagnostics,
                    )
                )

        return CandidateBatch(candidates=candidates, diagnostics=diagnostics, drops=drops)

    async def _bootstrap_draft(
        self,
        *,
        spec: AgentSpec,
        view: AttackGraphView,
        node: GraphNode,
        context: GraphCandidateContext | None,
        covered_hosts: set[str],
        endpoints_by_host: dict[str, list[GraphNode]],
        diagnostics: list[CandidateDiagnostic],
    ) -> CandidateDraft | None:
        facts = facts_of(node.properties)
        host = facts.get("host")
        if not isinstance(host, str) or not host:
            return None
        if not _http_family_service(facts):
            return None
        host_id = host_node_id(host)
        if str(host_id) in covered_hosts:
            return None
        host_node = await view.get_node(host_id)
        if host_node is None:
            # The plan requires the corresponding HOST to exist; a SERVICE
            # without its parent is not something this provider will patch up.
            return None
        if endpoints_by_host.get(host.strip().lower()):
            return None
        target_ref = _target_ref(facts, facts_of(host_node.properties))
        if target_ref is None:
            return None
        if context is not None and not context.can_execute(
            "recon", _SERVICE_DISCOVERY_OPERATION
        ):
            diagnostics.append(
                CandidateDiagnostic(
                    target_node_id=host_id,
                    reason="no_executable_service_discovery_tool",
                    evidence_id=str(node.id),
                )
            )
            return None

        return CandidateDraft(
            agent_id=spec.agent_id,
            plan_step=PlanStep(
                step_id=f"bootstrap-service:{node.id}",
                capability="recon",
                operation=_SERVICE_DISCOVERY_OPERATION,
                tool_id=RECON_HTTP_PROBE,
                compatible_tool_ids=[RECON_HTTP_PROBE],
                target=HostTarget(host=host, graph_node_id=host_id),
                arguments={"target_ref": target_ref, "path": _BOOTSTRAP_PATH},
                reason=(
                    "An HTTP service was observed without any endpoint, so nothing "
                    "downstream can proceed; the existing probe recovers the surface"
                ),
                hypothesis=HypothesisDraft(
                    description=(
                        f"The observed {facts.get('application_protocol', 'http')} service "
                        f"on {host} answers a first request"
                    ),
                    confidence=0.5,
                ),
            ),
            target_node_ids=[host_id],
            conflict_keys=[f"target:{host_id}"],
            signals=PrioritySignals(
                expected_information_gain=0.8,
                downstream_unblock_count=2,
                expected_cost=0.2,
                source_refs=[f"graph:{node.id}"],
            ),
        )

    async def _inventory_drafts(
        self,
        *,
        spec: AgentSpec,
        view: AttackGraphView,
        node: GraphNode,
        context: GraphCandidateContext | None,
        diagnostics: list[CandidateDiagnostic],
    ) -> list[CandidateDraft]:
        facts, attrs = facts_of(node.properties), attrs_of(node.properties)
        host, path = facts.get("host"), facts.get("path")
        if not isinstance(host, str) or not host:
            return []
        if not isinstance(path, str) or not path.startswith("/"):
            return []
        host_node = await view.get_node(host_node_id(host))
        target_ref = _target_ref(facts, facts_of(host_node.properties) if host_node else {})
        if target_ref is None:
            return []
        if context is not None and not context.can_execute(
            "recon", _ENDPOINT_DISCOVERY_OPERATION
        ):
            return []

        # html_inventory and openapi_inventory map through _common_context,
        # which reads host/port off any target that has them and uses
        # node_id_for_target(target) as the source of the discovery edge -- so
        # an EndpointTarget is both correct and more precise (the edge starts
        # at the endpoint that was inspected).
        endpoint_target = EndpointTarget(
            host=host,
            path=path,
            port=facts.get("port"),
            graph_node_id=node.id,
        )
        # oidc_inventory maps through _host_context, which requires a
        # HostTarget and otherwise returns nothing at all -- verified directly
        # against the Mapper. Its execution target is therefore the HOST, and
        # the observed ENDPOINT travels as provenance instead.
        host_target = HostTarget(host=host, graph_node_id=host_node_id(host))

        drafts: list[CandidateDraft] = []
        statuses = _observed_statuses(facts, attrs)
        if _HTML_INVENTORY_STATUS in statuses:
            drafts.append(
                self._inventory_draft(
                    spec=spec,
                    node=node,
                    target=endpoint_target,
                    target_ref=target_ref,
                    path=path,
                    tool_id=RECON_HTML_INVENTORY,
                    step_prefix="html-inventory",
                    reason=(
                        "The endpoint answered successfully, so its own links and forms "
                        "are a real surface to enumerate"
                    ),
                    question=f"{path} serves HTML that references further same-origin paths",
                    signals=PrioritySignals(
                        expected_information_gain=0.6,
                        downstream_unblock_count=2,
                        expected_cost=0.2,
                        source_refs=[f"graph:{node.id}"],
                    ),
                )
            )
        if _looks_like_api_document(path, _OPENAPI_PATH_MARKERS):
            drafts.append(
                self._inventory_draft(
                    spec=spec,
                    node=node,
                    target=endpoint_target,
                    target_ref=target_ref,
                    path=path,
                    tool_id=RECON_OPENAPI_INVENTORY,
                    step_prefix="openapi-inventory",
                    reason=(
                        "This observed path names an API description document, so its "
                        "declared operations are worth reading"
                    ),
                    question=f"{path} describes operations the graph does not know yet",
                    signals=PrioritySignals(
                        expected_information_gain=0.8,
                        downstream_unblock_count=3,
                        expected_cost=0.2,
                        source_refs=[f"graph:{node.id}"],
                    ),
                )
            )
        if _looks_like_api_document(path, _OIDC_PATH_MARKERS):
            drafts.append(
                self._inventory_draft(
                    spec=spec,
                    node=node,
                    target=host_target,
                    target_ref=target_ref,
                    path=path,
                    tool_id=RECON_OIDC_INVENTORY,
                    step_prefix="oidc-inventory",
                    reason=(
                        "This observed path names an OIDC discovery document, so its "
                        "declared capabilities are worth reading"
                    ),
                    question=f"{path} advertises identity capabilities the graph does not know",
                    signals=PrioritySignals(
                        expected_information_gain=0.7,
                        downstream_unblock_count=2,
                        expected_cost=0.2,
                        source_refs=[f"graph:{node.id}"],
                    ),
                )
            )
        if not drafts and not statuses:
            diagnostics.append(
                CandidateDiagnostic(
                    target_node_id=node.id,
                    reason="endpoint_never_responded",
                    evidence_id=str(node.id),
                )
            )
        return drafts

    def _inventory_draft(
        self,
        *,
        spec: AgentSpec,
        node: GraphNode,
        target: AnyTarget,
        target_ref: str,
        path: str,
        tool_id: ToolId,
        step_prefix: str,
        reason: str,
        question: str,
        signals: PrioritySignals,
    ) -> CandidateDraft:
        # Derived from the execution target rather than hand-paired with it,
        # so an execution target and a graph target can never disagree again:
        # CandidateCompiler rejects exactly this discrepancy as
        # target_mismatch.
        target_node_id = node_id_for_target(target)
        return CandidateDraft(
            agent_id=spec.agent_id,
            plan_step=PlanStep(
                step_id=f"{step_prefix}:{node.id}",
                capability="recon",
                operation=_ENDPOINT_DISCOVERY_OPERATION,
                tool_id=tool_id,
                compatible_tool_ids=[tool_id],
                # Listing is an observation step: it may find new paths, and it
                # must not be able to claim a vulnerability by itself.
                assessment_kind="observation",
                target=target,
                arguments={"target_ref": target_ref, "path": path},
                reason=reason,
                hypothesis=HypothesisDraft(
                    description=question,
                    confidence=0.3,
                    metadata={
                        "kind": "surface_discovery",
                        # The observed endpoint that justified this option.
                        "source_node_id": str(node.id),
                        "source_path": path,
                    },
                ),
            ),
            target_node_ids=[target_node_id],
            conflict_keys=[f"target:{target_node_id}"],
            signals=signals,
        )

    async def _tls_draft(
        self,
        *,
        spec: AgentSpec,
        view: AttackGraphView,
        node: GraphNode,
        context: GraphCandidateContext | None,
    ) -> CandidateDraft | None:
        facts, attrs = facts_of(node.properties), attrs_of(node.properties)
        host = facts.get("host")
        if not isinstance(host, str) or not host:
            return None
        if not _observed_https_service(facts, attrs):
            return None
        host_node = await view.get_node(host_node_id(host))
        target_ref = _target_ref(facts, facts_of(host_node.properties) if host_node else {})
        if target_ref is None:
            return None
        if context is not None and not context.can_execute(
            "recon", _SERVICE_DISCOVERY_OPERATION
        ):
            return None
        # tls_inspect handshakes the target's origin, so the execution target
        # is the HOST: its Mapper requires a HostTarget and publishes the
        # result under the host's "tls" SERVICE. The SERVICE node that
        # justified this candidate is carried as provenance, not as the
        # execution target -- naming it in target_node_ids would resolve to a
        # different node than the target and the compiler would reject the
        # draft as target_mismatch.
        host_id = host_node_id(host)
        return CandidateDraft(
            agent_id=spec.agent_id,
            plan_step=PlanStep(
                step_id=f"tls-inspect:{node.id}",
                capability="recon",
                operation=_SERVICE_DISCOVERY_OPERATION,
                tool_id=RECON_TLS_INSPECT,
                compatible_tool_ids=[RECON_TLS_INSPECT],
                assessment_kind="observation",
                target=HostTarget(host=host, graph_node_id=host_id),
                arguments={"target_ref": target_ref},
                reason=(
                    "This service was observed over HTTPS, so its negotiated protocol, "
                    "cipher and certificate can be measured directly"
                ),
                hypothesis=HypothesisDraft(
                    description=f"{host} presents a verifiable TLS configuration",
                    confidence=0.4,
                    metadata={
                        "kind": "transport_security",
                        "source_node_id": str(node.id),
                    },
                ),
            ),
            target_node_ids=[host_id],
            conflict_keys=[f"target:{host_id}"],
            signals=PrioritySignals(
                expected_information_gain=0.3,
                expected_cost=0.2,
                source_refs=[f"graph:{node.id}"],
            ),
        )
