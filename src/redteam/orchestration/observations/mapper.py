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

import importlib
import logging
from collections.abc import Callable, Iterable
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
from redteam.orchestration.observations.reference_scan import TargetReference

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

    # Reference candidates are a separate channel from nodes/edges/findings:
    # addresses this run preserved origin mentioned, never synthesized
    # facts. UPDATE stores them as an evidence metadata companion only and
    # never turns them into graph nodes or facts.
    target_references: list[TargetReference] = field(default_factory=list)


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

    for entry in mutation.target_references:
        if not isinstance(entry, TargetReference):
            raise ValueError("mapper emitted a non-TargetReference entry")
        if not entry.raw_path or entry.raw_path[0] != "output":
            raise ValueError("reference raw_path must start at output")
        if getattr(entry.target, "kind", None) == "graph_node":
            raise ValueError("references must not assert graph nodes")
        if getattr(entry.target, "graph_node_id", None) is not None:
            raise ValueError("references must not assert node identity")
    node_types: dict[GraphNodeId, GraphNodeType] = {}
    for node in mutation.nodes:
        encoded_type, _ = split_node_id(node.node_id)
        if encoded_type is not node.node_type:
            raise ValueError(
                f"node id {node.node_id!r} encodes {encoded_type.value}, not {node.node_type.value}"
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
        self._change_keys: dict[str, tuple[str, ...]] = {}

    @property
    def last_failure(self) -> MapperFailure | None:
        return self._last_failure

    def has_mapper(self, tool_id: str) -> bool:
        return tool_id in self._mappers

    def register(self, tool_id: str, mapper: ResultMapperFn, *, meaningful_change_keys=()) -> None:
        if tool_id in self._mappers:
            raise ValueError(f"a result mapper is already registered for {tool_id!r}")
        self._mappers[tool_id] = mapper
        self._change_keys[tool_id] = tuple(meaningful_change_keys)

    def bind_catalog(self, catalog) -> None:
        """Resolve existing explicit mapper aliases through the selected catalog."""
        for tool_id, mapper in tuple(self._mappers.items()):
            try:
                descriptor = catalog.get(ToolId(tool_id))
            except ValueError:
                continue  # An ambiguous old alias proves no output connection.
            if descriptor is not None and str(descriptor.tool_id) != tool_id:
                resolved = str(descriptor.tool_id)
                if resolved not in self._mappers:
                    self.register(
                        resolved,
                        mapper,
                        meaningful_change_keys=self.meaningful_change_keys(tool_id),
                    )

    def meaningful_change_keys(self, tool_id: str) -> tuple[str, ...]:
        """Extra fingerprint keys one mapper's bundle declared (M18)."""
        return self._change_keys.get(tool_id, ())

    def all_meaningful_change_keys(self) -> dict[str, tuple[str, ...]]:
        """Every declared key list, by tool id, for the fingerprint reader."""
        return dict(self._change_keys)

    def map_result(
        self,
        *,
        tool_id: ToolId,
        target: AnyTarget,
        arguments: dict[str, Any],
        result: ToolResult,
    ) -> GraphMutation:
        return self.map_result_with_outcome(
            tool_id=tool_id,
            target=target,
            arguments=arguments,
            result=result,
        ).mutation

    def map_result_with_outcome(
        self,
        *,
        tool_id: ToolId,
        target: AnyTarget,
        arguments: dict[str, Any],
        result: ToolResult,
    ) -> MappingOutcome:
        """Map one preserved result with its per-call outcome.

        Status is mapped when a mapper ran clean, empty when nothing was
        discovered, no_mapper when no mapper serves the tool, and failed
        when the mapper raised. The shared last_failure slot keeps its
        existing meaning for current readers.
        """
        self._last_failure = None
        mapper = self._mappers.get(str(tool_id))
        if mapper is None:
            return MappingOutcome(
                mutation=GraphMutation(),
                tool_id=str(tool_id),
                mapper=None,
                status="no_mapper",
            )
        name = getattr(mapper, "__name__", type(mapper).__name__)
        try:
            mutation = mapper(target=target, arguments=arguments, result=result)
            _validate_mutation(mutation, target=target)
        except Exception as exc:  # noqa: BLE001 - mapper failure is observable, not fatal
            self._last_failure = MapperFailure(
                tool_id=str(tool_id),
                mapper=name,
                reason=type(exc).__name__,
            )
            logger.warning("result mapper for %s raised; ignoring", tool_id, exc_info=True)
            return MappingOutcome(
                mutation=GraphMutation(),
                tool_id=str(tool_id),
                mapper=name,
                status="failed",
                reason=type(exc).__name__,
            )
        if not mutation.nodes and not mutation.edges and not mutation.findings and not mutation.target_references:
            return MappingOutcome(
                mutation=mutation,
                tool_id=str(tool_id),
                mapper=name,
                status="empty",
            )
        return MappingOutcome(
            mutation=mutation,
            tool_id=str(tool_id),
            mapper=name,
            status="mapped",
        )

@dataclass(frozen=True)
class MappingOutcome:
    """Immutable per-call mapping result.

    The shared last_failure slot stays for existing readers, but the
    outcome itself travels with the call: status is mapped when a mapper
    ran clean, empty when nothing was discovered, no_mapper when no
    mapper serves the tool, and failed when the mapper raised.
    """

    mutation: GraphMutation
    tool_id: str
    mapper: str | None
    status: str
    reason: str | None = None

#: Contribution kind spoken by observation bundles. The composition
#: root owns collection; the name is the draft so both sides can meet.
RESULT_MAPPER_KIND = "result_mapper"


@dataclass(frozen=True)
class MapperContribution:
    """One mapper's declaration: tools served, code reference, version,
    and the extra fingerprint keys (M05/M18).

    ``mapper_ref`` is a ``dotted.module:attr`` import reference resolved
    only at assembly time -- the live function itself is never stored in
    declaration metadata. ``meaningful_change_keys`` holds graph
    attribute/fact keys beyond the shared planning allow-list whose
    change wakes the owning role; empty means no extra contribution,
    never a block on running the tool.
    """

    tool_ids: tuple[str, ...]
    mapper_ref: str
    mapper_version: str
    meaningful_change_keys: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.tool_ids or not self.mapper_ref.strip() or not self.mapper_version.strip():
            raise ValueError("mapper contribution identity must not be blank")


def resolve_contribution_mapper(contribution: MapperContribution) -> ResultMapperFn:
    """Import the mapper a bundle names; fails loudly when unresolvable."""
    module_name, sep, attr = contribution.mapper_ref.partition(":")
    if not sep or not module_name.strip() or not attr.strip():
        raise ValueError(f"invalid mapper reference {contribution.mapper_ref!r}")
    try:
        module = importlib.import_module(module_name)
        mapper = getattr(module, attr)
    except (ImportError, AttributeError) as exc:
        raise ValueError(f"unresolvable mapper reference {contribution.mapper_ref!r}") from exc
    if not callable(mapper):
        raise ValueError(f"mapper reference is not callable {contribution.mapper_ref!r}")
    return mapper


def register_contribution(
    registry: ResultMapperRegistry, contribution: MapperContribution
) -> MapperContribution:
    """Register one bundle's mapper under every tool id it serves.

    Exact-id matching, duplicate rejection and failure isolation stay
    exactly as ``register``/``map_result`` define them; this only adds
    the shared assembly path plus the fingerprint key bookkeeping. A
    missing mapper never blocks tool execution -- ``map_result`` still
    returns an empty change for unmapped tools.
    """
    mapper = resolve_contribution_mapper(contribution)
    for tool_id in contribution.tool_ids:
        registry.register(
            tool_id, mapper, meaningful_change_keys=contribution.meaningful_change_keys
        )
    return contribution


def _mapper_contribution_id(contribution: MapperContribution) -> str:
    """Single tool id today; sorted join only if a bundle ever serves several."""
    if len(contribution.tool_ids) == 1:
        return contribution.tool_ids[0]
    return "|".join(sorted(contribution.tool_ids))


def mapper_contribution(contribution: MapperContribution):
    """One bundle in the composition root's declaration shape.

    The factory returns the resolved mapper callable (the runtime kind
    contract), never the declaration object itself.
    """
    from redteam.core.extensions import Contribution

    def _resolved_mapper(_context: Any) -> Any:
        return resolve_contribution_mapper(contribution)

    return Contribution(
        kind=RESULT_MAPPER_KIND,
        contribution_id=_mapper_contribution_id(contribution),
        version=contribution.mapper_version,
        factory=_resolved_mapper,
        meaningful_change_keys=contribution.meaningful_change_keys,
        target_ids=contribution.tool_ids,
    )


def mapper_declarations_by_contribution_id(
    packages: Iterable[MapperContribution] | None = None,
) -> dict[str, MapperContribution]:
    """Declarations keyed by contribution id, for full-declaration assembly.

    Path (b): the root looks a declaration up here and calls
    register_contribution() so fingerprint keys are recorded too.
    """
    from redteam.orchestration.observations.packages import builtin_observation_packages

    selected = tuple(packages) if packages is not None else builtin_observation_packages()
    declarations: dict[str, MapperContribution] = {}
    for package in selected:
        declarations[_mapper_contribution_id(package)] = package
    return declarations


def load_mapper_contribution(
    contribution_id: str,
    packages: Iterable[MapperContribution] | None = None,
) -> MapperContribution:
    """One declaration by contribution id; unknown ids fail loudly."""
    try:
        return mapper_declarations_by_contribution_id(packages)[contribution_id]
    except KeyError:
        raise ValueError(f"unknown mapper contribution {contribution_id!r}") from None


def default_result_mapper_registry() -> ResultMapperRegistry:
    """Assembled from the built-in observation bundles (M05).

    Same mappers under the same exact tool ids as before; the bundles
    only add the shared assembly path plus fingerprint key records.
    The conditional MobSF mapper stays out: the mobile wiring adds it
    from its bundle when the tool exists.
    """
    from redteam.orchestration.observations.packages import builtin_observation_packages

    registry = ResultMapperRegistry()
    for package in builtin_observation_packages():
        register_contribution(registry, package)
    return registry
