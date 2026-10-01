"""``GraphMutation``, the ``ResultMapper`` Protocol, and the registry that
looks one up by ``tool_id``.

A mapper never touches the store: it only *describes* what should be
upserted (:class:`NodeMutation`/:class:`EdgeMutation`), so it needs no
``AttackGraphService``, has nothing to mock beyond a ``ToolResult``, and
cannot itself cause a duplicate write -- deduplication is
``AttackGraphService.upsert_node``/``link``'s job, keyed on the
deterministic node/edge ids a mapper computes (``attack_graph.identity``).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, TypeGuard

from redteam.attack_graph.identity import node_id_for_target, split_node_id
from redteam.attack_graph.predicates import (
    validate_attrs,
    validate_facts,
    validate_properties,
)
from redteam.core.contracts.target import AnyTarget
from redteam.core.contracts.tool import ToolResult
from redteam.core.enums import GraphEdgeType, GraphNodeType, Verdict
from redteam.core.ids import GraphNodeId, ToolId
from redteam.orchestration.expectations import Expectation

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class NodeMutation:
    node_type: GraphNodeType
    node_id: GraphNodeId
    attrs: dict[str, Any] = field(default_factory=dict)
    facts: dict[str, Any] = field(default_factory=dict)
    labels: list[str] = field(default_factory=list)
    observations: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class MapperFailure:
    tool_id: str
    mapper: str
    reason: str


@dataclass(frozen=True)
class EdgeMutation:
    edge_type: GraphEdgeType
    source_id: GraphNodeId
    target_id: GraphNodeId
    properties: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class FindingMutation:
    """Evidence-independent finding description produced by a pure mapper.

    UPDATE attaches the Evidence created by VERIFY before writing the
    Hypothesis and verified Finding into the Attack Graph.
    """

    check_id: str
    hypothesis_id: str
    finding_id: str
    title: str
    description: str
    severity: str
    expectation: Expectation
    verdict: Verdict
    reason: str
    target_node_id: GraphNodeId
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class GraphMutation:
    """What a mapper discovered. Empty means "nothing new" -- UPDATE's
    existing target-node write (``step.observed_attrs``) is unaffected
    either way; this is purely additive."""

    nodes: list[NodeMutation] = field(default_factory=list)
    edges: list[EdgeMutation] = field(default_factory=list)
    findings: list[FindingMutation] = field(default_factory=list)


def is_http_status(value: Any) -> TypeGuard[int]:
    """A real HTTP status code.

    Uses type(value) is int rather than isinstance, because True is an int
    subclass: a boolean where a status belongs means the tool output is not
    shaped the way the mapper was written for.
    """

    return type(value) is int and 100 <= value <= 599


#: A mapper only ever reads its inputs; it must not raise for
#: malformed/incomplete output (section 11 of the spec this implements)
#: -- the registry below also catches anything that slips through, but a
#: well-behaved mapper returns an empty ``GraphMutation`` itself instead
#: of relying on that safety net.
ResultMapperFn = Callable[..., GraphMutation]


def _validate_mutation(mutation: GraphMutation, *, target: AnyTarget) -> None:
    """Reject a whole mapper batch before UPDATE can apply it partially."""

    node_types: dict[GraphNodeId, GraphNodeType] = {}
    for node in mutation.nodes:
        encoded_type, _ = split_node_id(node.node_id)
        if encoded_type is not node.node_type:
            raise ValueError(
                f"node id {node.node_id!r} encodes {encoded_type.value}, "
                f"not {node.node_type.value}"
            )
        previous = node_types.setdefault(node.node_id, node.node_type)
        if previous is not node.node_type:
            raise ValueError(f"conflicting node types for {node.node_id!r}")
        validate_attrs(node.attrs)
        facts = dict(node.facts)
        if node.observations:
            facts["observations"] = node.observations
        validate_facts(facts)
    for edge in mutation.edges:
        split_node_id(edge.source_id)
        split_node_id(edge.target_id)
        validate_properties(edge.properties)
        known_ids = set(node_types) | {node_id_for_target(target)}
        if edge.source_id not in known_ids or edge.target_id not in known_ids:
            raise ValueError("mapper emitted an edge whose endpoint is not in its mutation")
    target_node_id = node_id_for_target(target)
    for finding in mutation.findings:
        if finding.target_node_id != target_node_id:
            raise ValueError("mapper emitted a finding for a different target")
        if finding.verdict is not Verdict.VIOLATION:
            raise ValueError("mapper finding must represent a verified violation")
        if not all(
            value.strip()
            for value in (
                finding.check_id,
                finding.hypothesis_id,
                finding.finding_id,
                finding.title,
                finding.description,
                finding.severity,
                finding.reason,
            )
        ):
            raise ValueError("mapper finding fields must not be blank")
        validate_facts(
            {
                "expectation": finding.expectation.model_dump(mode="json"),
                "metadata": finding.metadata,
            }
        )


class ResultMapperRegistry:
    """Exact ``tool_id`` -> mapper. No wildcard/prefix matching: a mapper
    is written for one tool's real, known output shape, never guessed
    for a family of tools it hasn't been verified against."""

    def __init__(self) -> None:
        self._mappers: dict[str, ResultMapperFn] = {}
        self._last_failure: MapperFailure | None = None

    @property
    def last_failure(self) -> MapperFailure | None:
        return self._last_failure

    def register(self, tool_id: str, mapper: ResultMapperFn) -> None:
        if tool_id in self._mappers:
            raise ValueError(f"a result mapper is already registered for {tool_id!r}")
        self._mappers[tool_id] = mapper

    def map_result(
        self,
        *,
        tool_id: ToolId,
        target: AnyTarget,
        arguments: dict[str, Any],
        result: ToolResult,
    ) -> GraphMutation:
        self._last_failure = None
        mapper = self._mappers.get(str(tool_id))
        if mapper is None:
            return GraphMutation()
        try:
            mutation = mapper(target=target, arguments=arguments, result=result)
            _validate_mutation(mutation, target=target)
            return mutation
        except Exception as exc:  # noqa: BLE001 - mapper failure is observable, not fatal
            self._last_failure = MapperFailure(
                tool_id=str(tool_id),
                mapper=getattr(mapper, "__name__", type(mapper).__name__),
                reason=type(exc).__name__,
            )
            logger.warning("result mapper for %s raised; ignoring", tool_id, exc_info=True)
            return GraphMutation()


def default_result_mapper_registry() -> ResultMapperRegistry:
    from redteam.orchestration.observations.mobile_analysis import (
        map_apktool_decode,
        map_apktool_manifest,
    )
    from redteam.orchestration.observations.mobile_runtime import (
        map_mobile_crash_detail,
        map_mobile_crash_list,
        map_mobile_device_logs,
    )
    from redteam.orchestration.observations.recon_http import map_recon_http_probe
    from redteam.orchestration.observations.recon_inventory import (
        map_recon_html_inventory,
        map_recon_openapi_inventory,
    )
    from redteam.orchestration.observations.recon_network import (
        map_recon_dns_resolve,
        map_recon_oidc_inventory,
        map_recon_port_scan,
        map_recon_tls_inspect,
    )
    from redteam.orchestration.observations.web_http import (
        map_web_compare_anonymous,
        map_web_compare_sessions,
        map_web_http_request,
        map_web_http_security_posture,
    )
    from redteam.orchestration.observations.web_proxy import (
        map_web_nuclei_response_check,
        map_web_proxy_compare_sessions,
        map_web_proxy_get_flow,
        map_web_proxy_list_flows,
        map_web_proxy_mutate_json,
        map_web_proxy_replay_flow,
        map_web_zap_capture_snapshot,
    )

    registry = ResultMapperRegistry()
    registry.register("analysis.decode_apk", map_apktool_decode)
    registry.register("analysis.get_manifest", map_apktool_manifest)
    registry.register("mobile.mobile_get_device_logs", map_mobile_device_logs)
    registry.register("mobile.mobile_list_crashes", map_mobile_crash_list)
    registry.register("mobile.mobile_get_crash", map_mobile_crash_detail)
    registry.register("recon.dns_resolve", map_recon_dns_resolve)
    registry.register("recon.http_probe", map_recon_http_probe)
    registry.register("recon.tls_inspect", map_recon_tls_inspect)
    registry.register("recon.html_inventory", map_recon_html_inventory)
    registry.register("recon.openapi_inventory", map_recon_openapi_inventory)
    registry.register("recon.oidc_inventory", map_recon_oidc_inventory)
    registry.register("recon.port_scan", map_recon_port_scan)
    registry.register("web.http_request", map_web_http_request)
    registry.register("web.http_compare_sessions", map_web_compare_sessions)
    registry.register("web.http_compare_anonymous", map_web_compare_anonymous)
    registry.register("web.http_security_posture", map_web_http_security_posture)
    registry.register("web.proxy_list_flows", map_web_proxy_list_flows)
    registry.register("web.proxy_get_flow", map_web_proxy_get_flow)
    registry.register("web.proxy_replay_flow", map_web_proxy_replay_flow)
    registry.register("web.proxy_mutate_json", map_web_proxy_mutate_json)
    registry.register("web.proxy_compare_sessions", map_web_proxy_compare_sessions)
    registry.register("web.zap_capture_snapshot", map_web_zap_capture_snapshot)
    registry.register("web.nuclei_response_check", map_web_nuclei_response_check)
    return registry
