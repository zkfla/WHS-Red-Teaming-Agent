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
from redteam.orchestration.action_naming import (
    canonical_action_key,
    tool_matches_action_namespace,
)
from redteam.orchestration.agents.base import AgentSpec, PlanStep
from redteam.orchestration.agents.providers import PlaybookCandidateProvider
from redteam.orchestration.candidates.catalog import ToolCatalog
from redteam.orchestration.candidates.models import (
    CandidateDraft,
    ExecutableCandidate,
)
from redteam.orchestration.candidates.scoring import score_candidate
from redteam.orchestration.candidates.tool_selection import select_tool


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
                canonical_action_key(plan_step.capability, plan_step.operation)
                execution_target_id = node_id_for_target(plan_step.resolved_target())
            except ValueError:
                # Providers must emit concrete executable steps, never an
                # unresolved bind that could fail after a lease is acquired.
                continue
            if execution_target_id not in draft.target_node_ids:
                # Context, work/conflict accounting and evidence routing must
                # describe the same target that Policy and ToolGateway receive.
                continue
            tool = self._resolve_tool(plan_step)
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

    def _resolve_tool(self, plan_step: PlanStep) -> ToolDescriptor | None:
        preferred, capability, operation = (
            plan_step.tool_id,
            plan_step.capability,
            plan_step.operation,
        )
        if not tool_matches_action_namespace(preferred, capability):
            return None
        if self._catalog is None:
            # No live Registry wired (legacy/minimal-vertical-slice path,
            # e.g. the in-process ExampleMCPAdapter): nothing to select
            # from, so the provider's declared binding is the only option.
            return ToolDescriptor(
                tool_id=preferred,
                server_id="legacy-playbook",
                tool_name=str(preferred).rpartition(".")[2] or str(preferred),
                capability_tags=[capability],
                risk_level=RiskLevel.LOW,
                health_status=ToolHealthStatus.UNKNOWN,
            )

        # Action-aware selection: never pin to the provider's declared
        # ``tool_id``. Ask the live Registry which tools are actually
        # eligible for this canonical action, and pick deterministically —
        # see ``tool_selection.select_tool``. A tool that disappeared,
        # went unhealthy, or was never bound to this operation is simply
        # not offered; that is not distinguished from "no candidate" here
        # because no ``ActionProposal`` is produced for either case (Policy
        # never sees a proposal this step could not have executed).
        return select_tool(self._catalog.list_tools_for_action(capability, operation))

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
