"""Pluggable candidate providers independent from the central graph router."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from redteam.attack_graph.identity import node_id_for_target
from redteam.attack_graph.observation import GraphObservation
from redteam.attack_graph.view import AttackGraphView
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
    available_capabilities: tuple[str, ...] = ()
    open_hypotheses: tuple[str, ...] = ()
    prior_candidate_keys: frozenset[str] = frozenset()
    #: (capability, operation) pairs with at least one healthy tool bound to
    #: them in the live catalog. Only meaningful together with
    #: operations_verified.
    available_operations: tuple[tuple[str, str], ...] = ()
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


def planning_fingerprint(
    *,
    observation: GraphObservation,
    available_operations: Iterable[tuple[str, str]] = (),
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
        "observation_context_counts": observation.observation_context_counts,
        "open_hypotheses": observation.open_hypotheses,
        "findings": observation.findings,
        "attempted": observation.attempted,
        "available_operations": sorted([list(item) for item in available_operations]),
        "prior_candidate_keys": sorted(prior_candidate_keys),
    }
    encoded = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()[:24]

class GraphCandidateProvider(Protocol):
    async def propose_graph(
        self, spec: AgentSpec, context: GraphCandidateContext
    ) -> list[CandidateDraft] | CandidateBatch: ...


class CandidateProvider(Protocol):
    async def propose(
        self,
        spec: AgentSpec,
        view: AttackGraphView,
        observation: GraphObservation,
        *,
        step: int,
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

        return await self._propose(
            spec, context.view, context.observation, step=context.step
        )

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
        for plan_step in await available_steps(
            spec, view, observation, step=step, drops=drops
        ):
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

    def __init__(self, providers: Iterable[tuple[str, CandidateProvider]] = ()) -> None:
        self._providers: dict[str, CandidateProvider] = {}
        for provider_id, provider in providers:
            self.register(provider_id, provider)

    def register(self, provider_id: str, provider: CandidateProvider) -> CandidateProvider:
        key = provider_id.strip()
        if not key:
            raise ValueError("provider id may not be blank")
        if key in self._providers:
            raise DuplicateProviderError(f"candidate provider {key!r} is already registered")
        self._providers[key] = provider
        return provider

    def get(self, provider_id: str) -> CandidateProvider | None:
        return self._providers.get(provider_id)

    @property
    def provider_ids(self) -> list[str]:
        return sorted(self._providers)


def default_provider_registry() -> CandidateProviderRegistry:
    from redteam.orchestration.agents.recon import ReconCandidateProvider
    from redteam.orchestration.agents.web import WebCandidateProvider

    playbook = PlaybookCandidateProvider()
    # Every AgentDefinition declares a provider_id, so every declared id must
    # resolve even when the role is a non-schedulable seam (Analysis is the
    # verifier, Reporting the terminal projector): a registry that cannot
    # resolve a declared id turns "flip schedulable to True later" into an
    # AgentProviderFailed crash instead of a working role.
    return CandidateProviderRegistry(
        [
            ("playbook:recon", ReconCandidateProvider()),
            ("playbook:web_api", WebCandidateProvider()),
        ]
        + [
            (f"playbook:{role}", playbook)
            for role in ("mobile", "analysis", "reporting")
        ]
    )
