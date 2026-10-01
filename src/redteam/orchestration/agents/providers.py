"""Pluggable candidate providers independent from the central graph router."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from redteam.attack_graph.identity import node_id_for_target
from redteam.attack_graph.observation import GraphObservation
from redteam.attack_graph.predicates import attrs_of, facts_of
from redteam.attack_graph.view import AttackGraphView
from redteam.core.contracts.graph import GraphNode
from redteam.core.enums import GraphNodeType
from redteam.core.ids import GraphNodeId
from redteam.orchestration.agents.base import AgentSpec, available_steps

if TYPE_CHECKING:
    from redteam.orchestration.candidates.models import CandidateDraft


@dataclass(frozen=True)
class CandidateDiagnostic:
    target_node_id: GraphNodeId
    reason: str
    evidence_id: str


@dataclass(frozen=True)
class CandidateBatch:
    candidates: list[CandidateDraft]
    diagnostics: list[CandidateDiagnostic] = field(default_factory=list)
    drops: list[DraftDrop] = field(default_factory=list)


@dataclass(frozen=True)
class DraftDrop:
    """A playbook entry that matched real graph context but produced no draft.

    Distinct from CandidateRejection: there is no CandidateDraft to reject
    because a graph-derived argument could not be resolved at all. Reported
    through the same CandidateRejected event so the drop is observable
    rather than silent.
    """

    step_id: str
    target_node_id: str
    reason: str
    field: str = ""


def endpoint_path_explicitly_disallowed(node: GraphNode) -> bool:
    """Honor a negative path decision already observed from an MCP inventory.

    Discovery is not authorization. An absent flag stays unknown; a later
    explicit positive observation can supersede an earlier negative one.
    The MCP server remains the final path/method/network enforcement point.
    """

    if node.type is not GraphNodeType.ENDPOINT:
        return False
    attrs, facts = attrs_of(node.properties), facts_of(node.properties)
    return (
        attrs.get("path_allowlisted") is False and facts.get("path_allowlisted") is not True
    ) or (facts.get("path_allowlisted") is False and attrs.get("path_allowlisted") is not True)


@dataclass(frozen=True)
class GraphCandidateContext:
    """Bounded read-only context for graph-driven candidate providers.

    Everything a provider may plan from is named here explicitly: the
    current graph view, the role-scoped observation, unresolved
    hypotheses, already-attempted operations, prior candidate identities,
    and -- when the runtime has a live Tool Catalog -- which
    (capability, operation) pairs are actually executable right now.
    A provider that needs a fact not in this context should have that
    fact added here rather than reach around the seam.
    """

    view: AttackGraphView
    observation: GraphObservation
    step: int
    task_id: str | None = None
    task_source: str | None = None
    task_objective: str | None = None
    #: The operator's original/current Mission objective. Kept separate from
    #: the role-scoped task so the LLM receives both instead of losing the
    #: user's request during task decomposition.
    mission_objective: str | None = None
    task_target_node_ids: tuple[GraphNodeId, ...] = ()
    available_capabilities: tuple[str, ...] = ()
    open_hypotheses: tuple[str, ...] = ()
    prior_candidate_keys: frozenset[str] = frozenset()
    #: (capability, operation) pairs with at least one healthy tool bound to
    #: them in the live catalog. Only meaningful together with
    #: operations_verified.
    available_operations: tuple[tuple[str, str], ...] = ()
    #: Concrete healthy ToolIds in the live catalog.  Operations alone are
    #: intentionally not enough when several adapters implement the same
    #: intent (for example HTTPX and a browser navigator).
    available_tool_ids: tuple[str, ...] = ()
    #: Redacted, bounded metadata for the live MCP tools behind
    #: ``available_operations``. This lets an LLM choose arguments/tool ids
    #: from reality instead of guessing from an operation name.
    available_tools: tuple[Mapping[str, Any], ...] = ()
    #: True when available_operations was actually derived from a live Tool
    #: Catalog. False means "unknown", not "none": with no catalog wired in,
    #: the provider's declared ToolId binding is the only evidence of
    #: executability that exists (the same rule the compiler already
    #: documents), so callers keep the legacy permissive behaviour.
    operations_verified: bool = False
    #: Digest of the meaningful planning inputs (content-sensitive, excludes
    #: the global graph revision counter). Recorded so a replan can say *what*
    #: changed instead of only that a counter moved.
    planning_fingerprint: str = ""
    #: RC#2: target-linked evidence outcomes (status/error/summary dicts, as
    #: produced by ``project_evidence_excerpts``). Gives a generating provider
    #: the raw result material -- not just the graph summary -- so the model
    #: can reason past what a playbook anticipated. Dicts, not the model type,
    #: to keep this seam import-cycle free.
    evidence_excerpts: tuple[Mapping[str, Any], ...] = ()
    #: Bounded Control/tool/state feedback from prior turns. These are safe
    #: summaries, not raw output or hidden reasoning.
    runtime_observations: tuple[Mapping[str, Any], ...] = ()
    #: Redacted operator guidance that should influence strategy without ever
    #: expanding the set of known targets or bypassing candidate compilation.
    operator_guidance: tuple[str, ...] = ()

    def can_execute(self, capability: str, operation: str) -> bool:
        """Whether a candidate for this action could actually be executed.

        The provider uses this to avoid offering a tool-specific candidate
        whose canonical operation is not bound to any real tool -- the
        catalog, not a guess, decides. The compiler is still the authority
        that enforces it; this only keeps unusable drafts out of the queue.
        """

        if not self.operations_verified:
            return True
        return (capability, operation) in self.available_operations

    def has_tool(self, tool_id: str) -> bool:
        """Whether a concrete tool is available in a verified live catalog."""

        return self.operations_verified and tool_id in self.available_tool_ids


def planning_fingerprint(
    *,
    observation: GraphObservation,
    available_operations: Iterable[tuple[str, str]] = (),
    available_tool_ids: Iterable[str] = (),
    prior_candidate_keys: Iterable[str] = (),
) -> str:
    """Stable digest of what a planning turn could actually have planned from.

    Deliberately content-sensitive (content_digests) and deliberately
    independent of the graph revision counter: an unrelated node added
    elsewhere must not change this, and a changed fact on a node the role
    already knows must.
    """

    payload = {
        "nodes_by_type": observation.nodes_by_type,
        "node_counts": observation.node_counts,
        "content_digests": observation.content_digests,
        "edge_counts": observation.edge_counts,
        "relation_digests": observation.relation_digests,
        "observation_context_counts": observation.observation_context_counts,
        "open_hypotheses": observation.open_hypotheses,
        "findings": observation.findings,
        "attempted": observation.attempted,
        "available_operations": sorted([list(item) for item in available_operations]),
        "available_tool_ids": sorted(available_tool_ids),
        "prior_candidate_keys": sorted(prior_candidate_keys),
    }
    encoded = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()[:24]


class GraphCandidateProvider(Protocol):
    """The one candidate-provider contract (section 4 consolidation): every
    provider registered in ``CandidateProviderRegistry`` is expected to
    implement ``propose_graph``.

    This used to be two independently declared Protocols
    (``GraphCandidateProvider.propose_graph`` and a narrower
    ``CandidateProvider.propose(spec, view, observation, *, step=)``) for
    the same responsibility. They are merged into this one formal contract.
    A provider that only implements the narrower, three-argument shape
    (no :class:`GraphCandidateContext`) is still accepted -- ``plan_node``
    dispatches to it as a documented, minimal fallback for simple/plugin
    providers that do not need the richer context (see
    ``tests/integration/test_agent_loop.py``'s ``Provider``/
    ``BrokenProvider``) -- but that shape is no longer a second, equally
    authoritative Protocol declaration; conform to ``propose_graph`` unless
    there is a real reason not to.
    """

    async def propose_graph(
        self, spec: AgentSpec, context: GraphCandidateContext
    ) -> list[CandidateDraft] | CandidateBatch: ...


class PlaybookCandidateProvider:
    """Compatibility provider for the existing declarative playbooks."""

    async def propose(
        self,
        spec: AgentSpec,
        view: AttackGraphView,
        observation: GraphObservation,
        *,
        step: int,
    ) -> list[CandidateDraft]:
        return (await self._propose(spec, view, observation, step=step)).candidates

    async def propose_graph(
        self, spec: AgentSpec, context: GraphCandidateContext
    ) -> CandidateBatch:
        """Default graph-aware seam while retaining playbook semantics."""

        return await self._propose(spec, context.view, context.observation, step=context.step)

    async def _propose(
        self,
        spec: AgentSpec,
        view: AttackGraphView,
        observation: GraphObservation,
        *,
        step: int,
    ) -> CandidateBatch:
        from redteam.orchestration.candidates.models import CandidateDraft, PrioritySignals

        drops: list[tuple[str, str, str]] = []
        drafts: list[CandidateDraft] = []
        for plan_step in await available_steps(spec, view, observation, step=step, drops=drops):
            target_id = node_id_for_target(plan_step.resolved_target())
            drafts.append(
                CandidateDraft(
                    agent_id=spec.agent_id,
                    plan_step=plan_step,
                    target_node_ids=[target_id],
                    conflict_keys=[f"target:{target_id}"],
                    signals=PrioritySignals(source_refs=[f"graph:{target_id}"]),
                )
            )
        return CandidateBatch(
            candidates=drafts,
            drops=[
                DraftDrop(
                    step_id=step_id,
                    target_node_id=target_node_id,
                    reason="graph_prerequisite_missing",
                    field=field,
                )
                for step_id, target_node_id, field in drops
            ],
        )


class DuplicateProviderError(ValueError):
    pass


class CandidateProviderRegistry:
    """Provider lookup by stable id; adding a role never edits graph.py."""

    def __init__(self, providers: Iterable[tuple[str, GraphCandidateProvider]] = ()) -> None:
        self._providers: dict[str, GraphCandidateProvider] = {}
        for provider_id, provider in providers:
            self.register(provider_id, provider)

    def register(
        self, provider_id: str, provider: GraphCandidateProvider
    ) -> GraphCandidateProvider:
        key = provider_id.strip()
        if not key:
            raise ValueError("provider id may not be blank")
        if key in self._providers:
            raise DuplicateProviderError(f"candidate provider {key!r} is already registered")
        self._providers[key] = provider
        return provider

    def get(self, provider_id: str) -> GraphCandidateProvider | None:
        return self._providers.get(provider_id)

    @property
    def provider_ids(self) -> list[str]:
        return sorted(self._providers)


def default_provider_registry() -> CandidateProviderRegistry:
    from redteam.orchestration.agents.recon import ReconCandidateProvider
    from redteam.orchestration.agents.web import WebCandidateProvider

    playbook = PlaybookCandidateProvider()

    providers: list[tuple[str, GraphCandidateProvider]] = [
        ("playbook:recon", ReconCandidateProvider()),
        ("playbook:web_api", WebCandidateProvider()),
        *[(f"playbook:{role}", playbook) for role in ("mobile", "analysis", "reporting")],
    ]

    return CandidateProviderRegistry(providers)
