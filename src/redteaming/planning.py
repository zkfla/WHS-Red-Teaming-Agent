from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any

from .llm.client import ChatClient, LLMError
from .models import Scope, stable_id
from .scenarios import ScenarioRegistry, valid_technique_ids


ALLOWED_ACTIONS = {
    "asset_discovery",
    "service_identification",
    "http_surface_identification",
    "detector_analysis",
    "verification",
    "reporting",
}


@dataclass(frozen=True)
class PlanStep:
    action: str
    objective: str
    rationale: str
    requires_approval: bool = False
    techniques: tuple[str, ...] = ()


@dataclass(frozen=True)
class EngagementPlan:
    objective: str
    scope_hosts: tuple[str, ...]
    steps: tuple[PlanStep, ...]
    stop_conditions: tuple[str, ...]
    source: str
    id: str
    scenario: str = "authorized exposure assessment"
    scenario_id: str = "external-exposure"
    scenario_tactics: tuple[str, ...] = ()
    scenario_techniques: tuple[str, ...] = ()


class PlanPolicyError(ValueError):
    pass


class PlanPolicy:
    def validate(self, plan: EngagementPlan, scope: Scope) -> EngagementPlan:
        if tuple(scope.allowed_hosts) != plan.scope_hosts:
            raise PlanPolicyError("plan scope does not match the active authorization scope")
        if not plan.steps:
            raise PlanPolicyError("plan must contain at least one step")
        for step in plan.steps:
            if step.action not in ALLOWED_ACTIONS:
                raise PlanPolicyError(f"unsupported plan action: {step.action}")
            if step.requires_approval:
                raise PlanPolicyError(f"approval-gated action is not approved: {step.action}")
            if not valid_technique_ids(step.techniques):
                raise PlanPolicyError("plan contains malformed ATT&CK technique identifiers")
        registry = ScenarioRegistry()
        if plan.scenario_id not in {item.id for item in registry.all()}:
            raise PlanPolicyError(f"unknown engagement scenario: {plan.scenario_id}")
        scenario = registry.get(plan.scenario_id)
        if plan.scenario_tactics and tuple(plan.scenario_tactics) != scenario.tactics:
            raise PlanPolicyError("plan scenario tactics do not match registered metadata")
        if plan.scenario_techniques and tuple(plan.scenario_techniques) != scenario.techniques:
            raise PlanPolicyError("plan scenario techniques do not match registered metadata")
        disallowed = [step.action for step in plan.steps if step.action not in scenario.allowed_actions]
        if disallowed:
            raise PlanPolicyError(
                f"scenario {plan.scenario_id} does not allow actions: {', '.join(disallowed)}"
            )
        return plan


class EngagementPlanner:
    def __init__(self, client: ChatClient | None = None, policy: PlanPolicy | None = None) -> None:
        self.client = client
        self.policy = policy or PlanPolicy()

    def create(self, request: str, scope: Scope) -> EngagementPlan:
        if self.client is not None:
            try:
                candidate = self._from_llm(request, scope)
                return self.policy.validate(candidate, scope)
            except (LLMError, PlanPolicyError, ValueError, json.JSONDecodeError):
                pass
        return self.policy.validate(self._fallback(request, scope), scope)

    def _from_llm(self, request: str, scope: Scope) -> EngagementPlan:
        prompt = {
            "role": "user",
            "content": (
                "Return JSON only for an authorized red-team engagement plan. "
                "Allowed actions are asset_discovery, service_identification, "
                "http_surface_identification, detector_analysis, verification, reporting. "
                "Do not invent targets or credentials. Do not include exploit execution. "
                f"Scope={list(scope.allowed_hosts)} Request={request}"
            ),
        }
        raw = self.client.chat([prompt])  # type: ignore[union-attr]
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            raise ValueError("planner response did not contain JSON")
        data = json.loads(match.group(0))
        return self._from_data(data, scope, "llm")

    def _from_data(self, data: dict[str, Any], scope: Scope, source: str) -> EngagementPlan:
        steps = tuple(
            PlanStep(
                action=str(item["action"]),
                objective=str(item.get("objective", item["action"])),
                rationale=str(item.get("rationale", "")),
                requires_approval=bool(item.get("requires_approval", False)),
                techniques=tuple(str(value) for value in item.get("techniques", ())),
            )
            for item in data.get("steps", [])
        )
        scenario_id = str(data.get("scenario_id", "external-exposure"))
        scenario = ScenarioRegistry().get(scenario_id)
        return EngagementPlan(
            objective=str(data.get("objective", "Authorized red-team assessment")),
            scope_hosts=scope.allowed_hosts,
            steps=steps,
            stop_conditions=tuple(str(item) for item in data.get("stop_conditions", ("Scope changes", "Evidence is insufficient"))),
            source=source,
            id=stable_id("plan", source, *scope.allowed_hosts, str(data.get("objective", ""))),
            scenario=scenario.name,
            scenario_id=scenario.id,
            scenario_tactics=scenario.tactics,
            scenario_techniques=scenario.techniques,
        )

    def _fallback(self, request: str, scope: Scope) -> EngagementPlan:
        normalized = request.lower()
        authenticated = any(token in normalized for token in (
            "authenticated", "authorization", "access control", "로그인", "인증", "인가", "권한",
        ))
        scenario_id = "authenticated-web" if authenticated else "external-exposure"
        scenario_name = ("authenticated web authorization assessment"
                         if authenticated else "authorized exposure assessment")
        steps = (
            PlanStep("asset_discovery", "Discover authorized hosts and declared services", "Establish the asset baseline.", techniques=("T1595",)),
            PlanStep("service_identification", "Identify reachable services", "Prioritize protocol-specific checks.", techniques=("T1046",)),
            PlanStep("http_surface_identification", "Collect safe HTTP observations", "Create normalized evidence for detectors.", techniques=("T1595.002",)),
            PlanStep("detector_analysis", "Run registered vulnerability detectors", "Generate hypotheses only from observations."),
            PlanStep("verification", "Replay candidate checks", "Publish only reproducible evidence-backed findings."),
            PlanStep("reporting", "Render the engagement report", "Preserve evidence, scope, and reproduction steps."),
        )
        scenario = ScenarioRegistry().get(scenario_id)
        return EngagementPlan(
            objective=request.strip() or "Authorized red-team assessment",
            scope_hosts=scope.allowed_hosts,
            steps=steps,
            stop_conditions=("Target leaves Scope", "Rate or approval policy blocks an action", "Evidence is insufficient"),
            source="deterministic-fallback",
            id=stable_id("plan", "fallback", *scope.allowed_hosts, request),
            scenario=scenario.name,
            scenario_id=scenario.id,
            scenario_tactics=scenario.tactics,
            scenario_techniques=scenario.techniques,
        )
