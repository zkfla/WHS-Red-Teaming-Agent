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
from typing import Any

from redteam.core.contracts.target import AnyTarget
from redteam.core.contracts.tool import ToolResult
from redteam.core.enums import GraphEdgeType, GraphNodeType
from redteam.core.ids import GraphNodeId, ToolId

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
class GraphMutation:
    """What a mapper discovered. Empty means "nothing new" -- UPDATE's
    existing target-node write (``step.observed_attrs``) is unaffected
    either way; this is purely additive."""

    nodes: list[NodeMutation] = field(default_factory=list)
    edges: list[EdgeMutation] = field(default_factory=list)


#: A mapper only ever reads its inputs; it must not raise for
#: malformed/incomplete output (section 11 of the spec this implements)
#: -- the registry below also catches anything that slips through, but a
#: well-behaved mapper returns an empty ``GraphMutation`` itself instead
#: of relying on that safety net.
ResultMapperFn = Callable[..., GraphMutation]


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
            return mapper(target=target, arguments=arguments, result=result)
        except Exception as exc:  # noqa: BLE001 - mapper failure is observable, not fatal
            self._last_failure = MapperFailure(
                tool_id=str(tool_id),
                mapper=getattr(mapper, "__name__", type(mapper).__name__),
                reason=type(exc).__name__,
            )
            logger.warning("result mapper for %s raised; ignoring", tool_id, exc_info=True)
            return GraphMutation()


def default_result_mapper_registry() -> ResultMapperRegistry:
    from redteam.orchestration.observations.recon_http import map_recon_http_probe
    from redteam.orchestration.observations.web_http import map_web_http_request

    registry = ResultMapperRegistry()
    registry.register("recon.http_probe", map_recon_http_probe)
    registry.register("web.http_request", map_web_http_request)
    return registry
