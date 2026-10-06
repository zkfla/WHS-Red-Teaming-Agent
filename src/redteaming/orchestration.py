from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .auth import HttpIdentityProfile
from .events import Event
from .llm.client import ChatClient
from .planning import EngagementPlan, EngagementPlanner
from .recon.runner import ReconRun
from .runtime import LocalRuntime
from .models import Finding, Scope
from .hypotheses import HypothesisPrioritizer
from .objectives import ObjectiveGraph, ObjectiveStatus


class AgentRole(str, Enum):
    PLANNER = "planner"
    WORKER = "worker"
    VERIFIER = "verifier"
    PUBLISHER = "publisher"
    REPORTER = "reporter"


class ScenarioPolicyError(ValueError):
    """Raised when a selected scenario is missing its required operator input."""


@dataclass(frozen=True)
class OrchestrationResult:
    plan: EngagementPlan
    runs: tuple[ReconRun, ...]
    findings: tuple[Finding, ...]


class EngagementOrchestrator:
    """Deterministic role handoff around the policy-gated LocalRuntime."""

    def __init__(self, runtime: LocalRuntime, client: ChatClient | None = None) -> None:
        self.runtime = runtime
        self.planner = EngagementPlanner(client)

    def execute(self, request: str, scope: Scope,
                identities: tuple[HttpIdentityProfile, ...] = (),
                resume_plan: EngagementPlan | None = None,
                detector_names: tuple[str, ...] = ()) -> OrchestrationResult:
        hypothesis_orchestrator = getattr(self.runtime, "hypothesis_orchestrator", None)
        if hypothesis_orchestrator is not None:
            hypothesis_orchestrator.prioritizer = HypothesisPrioritizer(self.planner.client)
        if resume_plan is not None:
            if tuple(resume_plan.scope_hosts) != tuple(scope.allowed_hosts):
                raise ScenarioPolicyError("saved plan scope does not match the active authorization scope")
            plan = self.planner.policy.validate(resume_plan, scope)
            self.runtime.events.publish(Event("agent.role.started", {
                "role": AgentRole.PLANNER.value, "action": "resume",
            }))
            self.runtime.events.publish(Event("agent.role.completed", {
                "role": AgentRole.PLANNER.value, "action": "resume",
                "source": "snapshot",
                "scenario": plan.scenario_id,
            }))
        else:
            plan = self._role_plan(request, scope)
        if plan.scenario_id == "authenticated-web" and len(identities) < 2:
            self.runtime.events.publish(Event("agent.policy.blocked", {
                "scenario": plan.scenario_id,
                "reason": "at least two operator-provided identity profiles are required",
            }))
            raise ScenarioPolicyError(
                "authenticated-web requires at least two Identity profiles; register them with /identity"
            )
        runs: list[ReconRun] = []
        findings: list[Finding] = []
        objective_graph = ObjectiveGraph.from_steps(plan.steps)
        self.runtime.events.publish(Event("objective.graph.validated", {
            "count": len(objective_graph.objectives), "plan": plan.id,
        }))
        resumed_ids = {
            str(event.payload.get("id"))
            for event in getattr(self.runtime.events, "history", [])
            if event.name == "objective.completed" and event.payload.get("plan") == plan.id
        }
        for index, step in enumerate(plan.steps):
            objective_id = f"objective-{index + 1}-{step.action}"
            if objective_id in resumed_ids:
                self.runtime.events.publish(Event("objective.resumed", {
                    "id": objective_id, "action": step.action, "plan": plan.id,
                }))
                continue
            objective = objective_graph.transition(objective_id, ObjectiveStatus.IN_PROGRESS)
            self.runtime.events.publish(Event("objective.started", {
                "id": objective.id, "action": objective.action, "plan": plan.id,
            }))
            role = self._role_for_action(step.action)
            runtime_handles_verification = step.action in {"detector_analysis", "verification"}
            if not runtime_handles_verification:
                self.runtime.events.publish(Event("agent.role.started", {
                    "role": role.value, "action": step.action, "intent": step.objective,
                }))
            if step.action in {"asset_discovery", "service_identification"} and not runs:
                runs = self.runtime.run_network_recon(scope)
            elif step.action == "http_surface_identification":
                if not runs:
                    runs = self.runtime.run_network_recon(scope)
                if detector_names:
                    findings = self.runtime.run_http_assessment(
                        scope, runs, identities=identities, detector_names=detector_names
                    )
                else:
                    findings = self.runtime.run_http_assessment(scope, runs, identities=identities)
            elif step.action == "reporting":
                # Reporting is explicitly user-triggered by the CLI; the role
                # records readiness but never writes a report implicitly.
                self.runtime.events.publish(Event("report.ready", {"plan": plan.id}))
            if not runtime_handles_verification:
                self.runtime.events.publish(Event("agent.role.completed", {
                    "role": role.value, "action": step.action,
                }))
            evidence_store = getattr(self.runtime, "evidence", None)
            if evidence_store is None:
                # Minimal injected runtimes used by library callers may not
                # expose a domain EvidenceStore; retain an auditable event
                # reference without pretending it is a finding artifact.
                evidence_ids = (f"event:{objective_id}",)
            else:
                evidence_ids = tuple(item.id for item in evidence_store.items.values())
            completed = objective_graph.transition(objective_id, ObjectiveStatus.COMPLETED,
                                                   evidence_ids=evidence_ids[-1:] if evidence_ids else ())
            self.runtime.events.publish(Event("objective.completed", {
                "id": completed.id, "action": completed.action,
                "evidence_count": len(completed.evidence_ids), "plan": plan.id,
            }))
        return OrchestrationResult(plan, tuple(runs), tuple(findings))

    def _role_plan(self, request: str, scope: Scope) -> EngagementPlan:
        self.runtime.events.publish(Event("agent.role.started", {
            "role": AgentRole.PLANNER.value, "action": "planning",
        }))
        plan = self.planner.create(request, scope)
        exploration = getattr(self.runtime, "exploration", None)
        if exploration is not None:
            scenario_node = exploration.add("scenario", plan.scenario_id)
            asset_graph = getattr(self.runtime, "assets", None)
            for asset in getattr(asset_graph, "assets", {}).values():
                asset_node = exploration.add("asset", asset.id)
                exploration.link(scenario_node, "assesses", asset_node)
        self.runtime.events.publish(Event("agent.role.completed", {
            "role": AgentRole.PLANNER.value, "action": "planning", "source": plan.source,
            "scenario": plan.scenario_id,
        }))
        return plan

    @staticmethod
    def _role_for_action(action: str) -> AgentRole:
        if action in {"asset_discovery", "service_identification", "http_surface_identification"}:
            return AgentRole.WORKER
        if action in {"detector_analysis", "verification"}:
            return AgentRole.VERIFIER
        if action == "reporting":
            return AgentRole.REPORTER
        return AgentRole.WORKER
