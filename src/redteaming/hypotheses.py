from __future__ import annotations

from dataclasses import dataclass
import json
import os
import re
from typing import Protocol
from urllib.parse import parse_qsl, urlsplit

from .detection import HttpObservation
from .detector_registry import Detector
from .events import Event, EventBus
from .models import Scope, VulnerabilityCandidate, stable_id
from .llm.client import ChatClient, LLMError


@dataclass(frozen=True)
class Hypothesis:
    id: str
    agent: str
    category: str
    asset_ids: tuple[str, ...]
    rationale: str
    required_evidence: tuple[str, ...]
    next_action: str
    confidence: float


class HypothesisAgent(Protocol):
    name: str
    category: str
    detector: Detector

    def propose(self, observations: list[HttpObservation]) -> list[VulnerabilityCandidate]:
        ...


@dataclass(frozen=True)
class DetectorHypothesisAgent:
    """Adapter that turns a deterministic detector into an explicit sub-agent."""

    name: str
    category: str
    detector: Detector

    def propose(self, observations: list[HttpObservation]) -> list[VulnerabilityCandidate]:
        return self.detector.detect(observations)

    def to_hypothesis(self, candidate: VulnerabilityCandidate) -> Hypothesis:
        return Hypothesis(
            id=stable_id("hypothesis", self.name, candidate.id),
            agent=self.name,
            category=candidate.category,
            asset_ids=candidate.asset_ids,
            rationale=candidate.hypothesis,
            required_evidence=candidate.test_plan,
            next_action=candidate.test_plan[0] if candidate.test_plan else "replay the observation",
            confidence=candidate.confidence,
        )


@dataclass(frozen=True)
class HypothesisDecision:
    hypothesis: Hypothesis
    candidate: VulnerabilityCandidate
    accepted: bool
    reason: str


class HypothesisPrioritizer:
    """Optional LLM ranking over already-gated hypotheses only."""

    def __init__(self, client: ChatClient | None = None) -> None:
        self.client = client

    def order(self, decisions: tuple[HypothesisDecision, ...], scope: Scope,
              observations: list[HttpObservation] | None = None) -> tuple[HypothesisDecision, ...]:
        fallback = tuple(sorted(decisions, key=lambda item: item.hypothesis.confidence, reverse=True))
        if self.client is None or len(fallback) < 2:
            return fallback
        prompt_content = (
            "Rank only the supplied hypothesis IDs for safe authorized verification. "
            '{"ids":[...]} JSON only and do not invent IDs, targets, tools, or payloads. '
            f"Scope={list(scope.allowed_hosts)} Hypotheses="
            + json.dumps([
                {"id": item.hypothesis.id, "category": item.hypothesis.category,
                 "next_action": item.hypothesis.next_action,
                 "confidence": item.hypothesis.confidence}
                for item in fallback
            ])
            + " ObservationSummary="
            + json.dumps({
                "count": len(observations or []),
                "status_codes": {
                    str(code): sum(1 for item in (observations or []) if item.status_code == code)
                    for code in sorted({item.status_code for item in (observations or [])})
                },
            })
        )
        if _observation_context_enabled():
            prompt_content += " ObservationContext=" + json.dumps(
                _safe_observation_context(observations or []), ensure_ascii=False
            )
        prompt = {
            "role": "user",
            "content": prompt_content,
        }
        try:
            raw = self.client.chat([prompt])
            match = re.search(r"\{.*\}", raw, re.DOTALL)
            data = json.loads(match.group(0)) if match else {}
            ranked = [str(value) for value in data.get("ids", [])]
            by_id = {item.hypothesis.id: item for item in fallback}
            ordered = [by_id[item] for item in ranked if item in by_id]
            ordered.extend(item for item in fallback if item.hypothesis.id not in ranked)
            return tuple(ordered)
        except (LLMError, ValueError, TypeError, json.JSONDecodeError):
            return fallback


def _observation_context_enabled() -> bool:
    """Require explicit operator opt-in before sending local observations to an LLM."""
    return os.getenv("REDTEAM_LLM_SEND_OBSERVATION_METADATA", "").strip().lower() in {
        "1", "true", "yes", "on"
    }


def _safe_observation_context(observations: list[HttpObservation]) -> list[dict[str, object]]:
    """Return signal-only context; never include bodies, header values, or evidence IDs."""
    context: list[dict[str, object]] = []
    for item in observations[:128]:
        parsed = urlsplit(item.object_key)
        headers = item.evidence.metadata.get("headers", {})
        header_names = sorted(str(key).lower() for key in headers) if isinstance(headers, dict) else []
        body = str(item.evidence.metadata.get("body_text", "")).lower()
        context.append({
            "asset_kind": item.asset.kind,
            "identity": item.identity,
            "path": parsed.path or "/",
            "query_keys": sorted({key for key, _ in parse_qsl(parsed.query, keep_blank_values=True)}),
            "status": item.status_code,
            "header_names": header_names,
            "signals": {
                "html": "text/html" in " ".join(header_names) or "<html" in body,
                "error_marker": item.status_code >= 500 and any(
                    marker in body for marker in ("traceback", "stack trace", "exception:", "sqlstate")
                ),
                "reflection_canary": "redteam-canary-" in body,
            },
        })
    return context


class HypothesisGate:
    """Policy boundary between proposal agents and verification workers."""

    allowed_actions = frozenset({"GET", "HEAD", "OPTIONS", "REPLAY", "COMPARE_IDENTITIES"})
    blocked_actions = frozenset({
        "POST", "PUT", "PATCH", "DELETE", "EXECUTE", "SHELL", "COMMAND", "UPLOAD",
    })

    def evaluate(self, hypothesis: Hypothesis, scope: Scope) -> tuple[bool, str]:
        if not hypothesis.asset_ids:
            return False, "hypothesis has no affected asset"
        if not hypothesis.required_evidence:
            return False, "hypothesis has no evidence requirement"
        action = hypothesis.next_action.upper()
        if any(token in action for token in self.blocked_actions):
            return False, "hypothesis requests a mutation or command execution"
        if not any(token in action for token in self.allowed_actions):
            return True, "action is descriptive and verifier will normalize it to a safe replay"
        return True, "scope and safe verification policy accepted"


class HypothesisOrchestrator:
    def __init__(self, agents: tuple[DetectorHypothesisAgent, ...] = (),
                 gate: HypothesisGate | None = None, events: EventBus | None = None,
                 prioritizer: HypothesisPrioritizer | None = None) -> None:
        self.agents = agents
        self.gate = gate or HypothesisGate()
        self.events = events
        self.prioritizer = prioritizer

    def generate(self, observations: list[HttpObservation], scope: Scope,
                 detector_names: tuple[str, ...]) -> tuple[HypothesisDecision, ...]:
        decisions: list[HypothesisDecision] = []
        for agent in self.agents:
            if agent.name not in detector_names:
                continue
            if self.events:
                self.events.publish(Event("hypothesis.agent.started", {
                    "agent": agent.name, "category": agent.category,
                }))
            for candidate in agent.propose(observations):
                hypothesis = agent.to_hypothesis(candidate)
                accepted, reason = self.gate.evaluate(hypothesis, scope)
                decision = HypothesisDecision(hypothesis, candidate, accepted, reason)
                decisions.append(decision)
                if self.events:
                    self.events.publish(Event("hypothesis.created", {
                        "agent": agent.name, "category": hypothesis.category,
                        "id": hypothesis.id, "accepted": accepted,
                    }))
                    self.events.publish(Event("hypothesis.gated", {
                        "id": hypothesis.id, "accepted": accepted, "reason": reason,
                    }))
            if self.events:
                self.events.publish(Event("hypothesis.agent.completed", {
                    "agent": agent.name, "category": agent.category,
                }))
        accepted = tuple(decision for decision in decisions if decision.accepted)
        ordered = self.prioritizer.order(accepted, scope, observations) if self.prioritizer else accepted
        if self.events and ordered:
            self.events.publish(Event("hypothesis.prioritized", {
                "count": len(ordered),
                "source": "llm" if self.prioritizer and self.prioritizer.client else "confidence",
            }))
        return ordered
