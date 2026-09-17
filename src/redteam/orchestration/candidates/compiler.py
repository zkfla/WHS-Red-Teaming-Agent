"""Legacy playbook -> validated, tool-bound, deterministically ranked candidates."""

from __future__ import annotations

import hashlib
import json
from functools import lru_cache

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError

from redteam.attack_graph.identity import node_id_for_target
from redteam.attack_graph.observation import GraphObservation
from redteam.attack_graph.view import AttackGraphView
from redteam.core.contracts.tool import ToolDescriptor
from redteam.core.enums import RiskLevel, ToolHealthStatus
from redteam.core.ids import ToolId
from redteam.orchestration.agents.base import AgentSpec
from redteam.orchestration.agents.providers import PlaybookCandidateProvider
from redteam.orchestration.candidates.catalog import ToolCatalog
from redteam.orchestration.candidates.models import (
    CandidateDraft,
    ExecutableCandidate,
)
from redteam.orchestration.candidates.scoring import score_candidate


@lru_cache(maxsize=256)
def _compiled_validator(schema_blob: str) -> Draft202012Validator:
    schema = json.loads(schema_blob)
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


class CandidateCompiler:
    """Compiles hard-constrained candidates; it does not own domain meaning."""

    def __init__(self, catalog: ToolCatalog | None = None) -> None:
        self._catalog = catalog

    async def compile_playbook(
        self,
        spec: AgentSpec,
        view: AttackGraphView,
        observation: GraphObservation,
        *,
        step: int,
    ) -> list[ExecutableCandidate]:
        drafts = await PlaybookCandidateProvider().propose(
            spec, view, observation, step=step
        )
        return self.compile_drafts(drafts)

    def compile_drafts(self, drafts: list[CandidateDraft]) -> list[ExecutableCandidate]:
        """Apply generic catalog/schema/health constraints to provider drafts."""

        candidates: list[ExecutableCandidate] = []
        seen: set[str] = set()
        for draft in drafts:
            plan_step = draft.plan_step
            try:
                execution_target_id = node_id_for_target(plan_step.resolved_target())
            except ValueError:
                # Providers must emit concrete executable steps, never an
                # unresolved bind that could fail after a lease is acquired.
                continue
            if execution_target_id not in draft.target_node_ids:
                # Context, work/conflict accounting and evidence routing must
                # describe the same target that Policy and ToolGateway receive.
                continue
            tool = self._resolve_tool(plan_step.tool_id, plan_step.capability)
            if tool is None or tool.health_status is ToolHealthStatus.UNHEALTHY:
                continue
            if not self._arguments_match_schema(plan_step.arguments, tool.input_schema):
                continue
            key = candidate_key(draft)
            if key in seen:
                continue
            seen.add(key)
            candidates.append(
                ExecutableCandidate(
                    candidate_key=key,
                    draft=draft,
                    tool=tool,
                    priority=score_candidate(
                        draft,
                        risk_level=tool.risk_level,
                        health_status=tool.health_status,
                    ),
                )
            )
        return sorted(
            candidates,
            key=lambda item: (-item.priority.total, item.candidate_key),
        )

    def _resolve_tool(self, preferred: ToolId, capability: str) -> ToolDescriptor | None:
        if self._catalog is None:
            return ToolDescriptor(
                tool_id=preferred,
                server_id="legacy-playbook",
                tool_name=str(preferred).rpartition(".")[2] or str(preferred),
                capability_tags=[capability],
                risk_level=RiskLevel.LOW,
                health_status=ToolHealthStatus.UNKNOWN,
            )

        exact = self._catalog.get(preferred)
        if exact is not None and capability in exact.capability_tags:
            return exact
        tools = [
            tool
            for tool in self._catalog.list_by_capability(capability)
            if tool.health_status is not ToolHealthStatus.UNHEALTHY
        ]
        if not tools:
            return None
        health_rank = {
            ToolHealthStatus.HEALTHY: 0,
            ToolHealthStatus.UNKNOWN: 1,
            ToolHealthStatus.UNHEALTHY: 2,
        }
        risk_rank = {
            RiskLevel.LOW: 0,
            RiskLevel.MEDIUM: 1,
            RiskLevel.HIGH: 2,
            RiskLevel.CRITICAL: 3,
        }
        return min(
            tools,
            key=lambda tool: (
                health_rank[tool.health_status],
                risk_rank[tool.risk_level],
                str(tool.tool_id),
            ),
        )

    @staticmethod
    def _arguments_match_schema(arguments: dict, schema: dict) -> bool:
        if not isinstance(schema, dict):
            return False
        try:
            # MCP arguments cross a JSON boundary even when a permissive
            # schema would otherwise accept Python-only values such as bytes.
            json.dumps(arguments, sort_keys=True, separators=(",", ":"))
            schema_blob = json.dumps(schema, sort_keys=True, separators=(",", ":"))
            _compiled_validator(schema_blob).validate(arguments)
        except (SchemaError, ValidationError, TypeError, ValueError):
            return False
        return True


def candidate_key(draft: CandidateDraft) -> str:
    """Stable identity intentionally excludes graph revision and score."""

    hypothesis = draft.plan_step.hypothesis
    payload = {
        "contract_version": draft.contract_version,
        "agent_id": str(draft.agent_id),
        "capability": draft.plan_step.capability,
        "operation": draft.plan_step.operation,
        "arguments": draft.plan_step.arguments,
        "target_node_ids": sorted(map(str, draft.target_node_ids)),
        "hypothesis": hypothesis.description if hypothesis else None,
        "prerequisite_fingerprint": draft.prerequisite_fingerprint,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return "cand_" + hashlib.sha256(encoded).hexdigest()[:24]
