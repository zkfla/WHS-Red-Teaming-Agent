"""Pluggable candidate providers independent from the central graph router."""

from __future__ import annotations

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
        from redteam.orchestration.candidates.models import CandidateDraft, PrioritySignals

        drafts: list[CandidateDraft] = []
        for plan_step in await available_steps(spec, view, observation, step=step):
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
        return drafts


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
    from redteam.orchestration.agents.web import WebCandidateProvider

    playbook = PlaybookCandidateProvider()
    return CandidateProviderRegistry(
        [(f"playbook:{role}", playbook) for role in ("recon", "mobile", "analysis")]
        + [("playbook:web_api", WebCandidateProvider())]
    )
