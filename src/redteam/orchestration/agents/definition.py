"""Extensible agent registration metadata.

Definitions describe *when* a role is relevant and which generic runtime
resources it may consume.  They deliberately contain no finance/mobile/web
domain objects: a role-specific candidate provider owns those semantics.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, model_validator

from redteam.attack_graph.observation import GraphObservation
from redteam.core.enums import AgentPhase, GraphEdgeType, GraphNodeType
from redteam.core.ids import AgentId


class ActivationRule(BaseModel):
    """Cheap graph-summary predicate used before a provider is invoked.

    The provider performs the authoritative, domain-specific filtering over
    the Attack Graph.  This rule only prevents obviously irrelevant roles
    from consuming a turn or an LLM call.
    """

    node_types: list[GraphNodeType] = Field(default_factory=list)
    phases: list[AgentPhase] = Field(default_factory=list)
    requires_open_hypothesis: bool = False
    requires_finding: bool = False

    @model_validator(mode="after")
    def _has_a_condition(self) -> ActivationRule:
        if not (
            self.node_types or self.phases or self.requires_open_hypothesis or self.requires_finding
        ):
            raise ValueError("an activation rule must declare at least one condition")
        return self

    def matches(self, observation: GraphObservation, phase: AgentPhase) -> bool:
        if self.phases and phase not in self.phases:
            return False
        if self.node_types and not any(
            observation.node_counts.get(node_type.value, 0) > 0 for node_type in self.node_types
        ):
            return False
        if self.requires_open_hypothesis and not observation.open_hypotheses:
            return False
        if self.requires_finding and not observation.findings:
            return False
        return True


class ContextProfile(BaseModel):
    """Declarative role-specific bounds consumed by ``ContextProjector``."""

    profile_id: str
    max_nodes: int = Field(default=40, ge=1)
    max_evidence: int = Field(default=20, ge=0)
    max_candidates: int = Field(default=40, ge=1)
    max_tokens: int = Field(default=8_000, ge=256)
    neighbor_depth: int = Field(default=1, ge=0, le=5)
    include_node_types: list[GraphNodeType] = Field(default_factory=list)
    #: Relation types this role plans from. Empty means the role does not
    #: subscribe to relation deltas at all and must not be woken by them.
    include_edge_types: list[GraphEdgeType] = Field(default_factory=list)
    include_hypotheses: bool = True
    include_findings: bool = True
    include_attempted: bool = True


class AgentDefinition(BaseModel):
    """Plugin metadata independent from scheduler state and LangGraph."""

    agent_id: AgentId
    role: str
    name: str
    description: str = ""
    contract_version: str = "1"
    #: ``False`` marks a seam role (Analysis=verifier, Reporting=projector)
    #: that has no executable ``AgentSpec`` and is never scheduled.
    schedulable: bool = True
    activation_rules: list[ActivationRule] = Field(default_factory=list)
    capabilities: list[str] = Field(default_factory=list)
    provider_id: str
    context_profile: ContextProfile
    planner_kind: str = "playbook"
    max_concurrency: int = Field(default=1, ge=1)
    budget_weight: float = Field(default=1.0, gt=0)
    priority_band: int = Field(default=100, ge=0)

    def is_active(self, observation: GraphObservation, phase: AgentPhase) -> bool:
        return not self.activation_rules or any(
            rule.matches(observation, phase) for rule in self.activation_rules
        )


def builtin_agent_definitions() -> list[AgentDefinition]:
    """The five agreed roles and their scheduler-facing metadata.

    Recon, Web/API and Mobile have executable specs/providers. Analysis is the
    verifier seam and Reporting is the terminal read-only projector, so neither
    consumes ordinary action-loop turns — both carry ``schedulable=False`` to
    say so in the model, matching the fact that they have no ``AgentSpec``.
    """

    return [
        AgentDefinition(
            agent_id=AgentId("recon-agent"),
            role="recon",
            name="Recon Agent",
            description="Discovers in-scope hosts, services and endpoints.",
            activation_rules=[ActivationRule(node_types=[GraphNodeType.HOST])],
            capabilities=["recon"],
            provider_id="playbook:recon",
            context_profile=ContextProfile(
                profile_id="recon",
                max_nodes=60,
                include_node_types=[
                    GraphNodeType.HOST,
                    GraphNodeType.SERVICE,
                    GraphNodeType.ENDPOINT,
                    GraphNodeType.HYPOTHESIS,
                ],
                include_findings=False,
            ),
            max_concurrency=4,
            priority_band=10,
        ),
        AgentDefinition(
            agent_id=AgentId("web-api-agent"),
            role="web_api",
            name="Web/API Agent",
            description="Analyzes discovered HTTP and API targets.",
            activation_rules=[
                ActivationRule(node_types=[GraphNodeType.ENDPOINT, GraphNodeType.SERVICE])
            ],
            capabilities=["web", "api"],
            provider_id="playbook:web_api",
            context_profile=ContextProfile(
                profile_id="web_api",
                include_node_types=[
                    GraphNodeType.HOST,
                    GraphNodeType.SERVICE,
                    GraphNodeType.ENDPOINT,
                    GraphNodeType.PRINCIPAL,
                    GraphNodeType.CREDENTIAL,
                    # What a principal can reach is a first-class part of the
                    # web/api surface: an access relation appearing between
                    # nodes the role already knows is new work.
                    GraphNodeType.RESOURCE,
                    GraphNodeType.HYPOTHESIS,
                    GraphNodeType.FINDING,
                ],
                include_edge_types=[
                    GraphEdgeType.CAN_ACCESS,
                    GraphEdgeType.OWNS,
                    GraphEdgeType.AUTHENTICATES_TO,
                    GraphEdgeType.OBSERVED,
                    GraphEdgeType.EXPOSES,
                ],
            ),
            max_concurrency=4,
            priority_band=20,
        ),
        AgentDefinition(
            agent_id=AgentId("mobile-agent"),
            role="mobile",
            name="Mobile Agent",
            description="Activates only when a provider resolves an APK/IPA artifact.",
            activation_rules=[ActivationRule(node_types=[GraphNodeType.ASSET])],
            capabilities=["mobile"],
            provider_id="playbook:mobile",
            context_profile=ContextProfile(
                profile_id="mobile",
                include_node_types=[
                    GraphNodeType.ASSET,
                    GraphNodeType.ENDPOINT,
                    GraphNodeType.HYPOTHESIS,
                    GraphNodeType.FINDING,
                ],
            ),
            max_concurrency=2,
            priority_band=30,
        ),
        AgentDefinition(
            agent_id=AgentId("analysis-agent"),
            role="analysis",
            name="Analysis Agent",
            description="Independently validates evidence and hypotheses.",
            schedulable=False,
            activation_rules=[
                ActivationRule(requires_open_hypothesis=True),
                ActivationRule(requires_finding=True),
            ],
            capabilities=["analysis"],
            provider_id="playbook:analysis",
            context_profile=ContextProfile(
                profile_id="analysis",
                max_evidence=40,
                include_node_types=[
                    GraphNodeType.HYPOTHESIS,
                    GraphNodeType.FINDING,
                ],
                include_attempted=False,
            ),
            max_concurrency=4,
            priority_band=40,
        ),
        AgentDefinition(
            agent_id=AgentId("reporting-agent"),
            role="reporting",
            name="Reporting Agent",
            description="Creates a read-only projection after drain/termination.",
            schedulable=False,
            activation_rules=[ActivationRule(phases=[AgentPhase.FINISHED, AgentPhase.FAILED])],
            capabilities=["reporting"],
            provider_id="playbook:reporting",
            context_profile=ContextProfile(
                profile_id="reporting",
                max_nodes=100,
                include_attempted=False,
            ),
            priority_band=1000,
        ),
    ]
