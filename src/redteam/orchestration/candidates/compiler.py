"""Legacy playbook -> validated, tool-bound, deterministically ranked candidates."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from functools import lru_cache
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError

from redteam.attack_graph.identity import node_id_for_target
from redteam.attack_graph.observation import GraphObservation
from redteam.attack_graph.view import AttackGraphView
from redteam.core.contracts.target import (
    AnyTarget,
    AssetTarget,
    DomainTarget,
    EndpointTarget,
    HostTarget,
    ServiceTarget,
    UrlTarget,
)
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


@dataclass(frozen=True)
class CandidateRejection:
    """Why one draft did not compile; ``field`` names the offending argument."""

    target_node_id: str
    capability: str
    operation: str
    tool_id: str
    reason: str
    field: str = ""
    candidate_key: str = ""


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
        return self.compile_drafts_with_rejections(drafts)[0]

    def compile_drafts_with_rejections(
        self, drafts: list[CandidateDraft]
    ) -> tuple[list[ExecutableCandidate], list[CandidateRejection]]:
        """Like :meth:`compile_drafts`, but also returns why each dropped draft
        was dropped (dedup drops are not reported)."""
        candidates: list[ExecutableCandidate] = []
        rejections: list[CandidateRejection] = []
        seen: set[str] = set()
        for draft in drafts:
            candidate, rejection = self._evaluate_draft(draft, seen)
            if candidate is not None:
                candidates.append(candidate)
            elif rejection is not None:
                rejections.append(rejection)
        return (
            sorted(candidates, key=lambda item: (-item.priority.total, item.candidate_key)),
            rejections,
        )

    def _evaluate_draft(
        self, draft: CandidateDraft, seen: set[str]
    ) -> tuple[ExecutableCandidate | None, CandidateRejection | None]:
        plan_step = draft.plan_step
        try:
            json.dumps(plan_step.arguments, sort_keys=True, allow_nan=False)
        except (TypeError, ValueError):
            return None, self._reject(draft, "non_json_arguments")
        try:
            canonical_action_key(plan_step.capability, plan_step.operation)
            execution_target_id = node_id_for_target(plan_step.resolved_target())
        except ValueError:
            return None, self._reject(draft, "unresolved_target")
        if execution_target_id not in draft.target_node_ids:
            return None, self._reject(draft, "target_mismatch")
        tool, tool_rejection = self._resolve_tool_with_reason(plan_step)
        if tool is None:
            reason, field = tool_rejection or ("no_compatible_tool", "")
            return None, self._reject(draft, reason, field=field)
        bound_arguments = self._bind_target_arguments(
            plan_step.arguments, plan_step.resolved_target(), tool, plan_step.operation
        )
        bound_step = plan_step.model_copy(update={"arguments": bound_arguments})
        bound_draft = draft.model_copy(update={"plan_step": bound_step})
        key = candidate_key(bound_draft)
        if key in seen:
            return None, self._reject(bound_draft, "duplicate_candidate")
        seen.add(key)
        return (
            ExecutableCandidate(
                candidate_key=key,
                draft=bound_draft,
                tool=tool,
                priority=score_candidate(
                    bound_draft,
                    risk_level=tool.risk_level,
                    health_status=tool.health_status,
                ),
            ),
            None,
        )

    @staticmethod
    def _reject(draft: CandidateDraft, reason: str, *, field: str = "") -> CandidateRejection:
        step = draft.plan_step
        try:
            diagnostic_key = candidate_key(draft)
        except (TypeError, ValueError):
            # A malformed/non-JSON candidate still needs an observable rejection,
            # but its invalid argument value must not escape into an event payload.
            # Hash only stable non-secret identity fields and argument *types*.
            safe_payload = {
                "agent_id": str(draft.agent_id),
                "capability": step.capability,
                "operation": step.operation,
                "target_node_ids": sorted(map(str, draft.target_node_ids)),
                "prerequisite_ids": sorted(draft.prerequisite_ids),
                "argument_types": {
                    str(key): type(value).__name__
                    for key, value in sorted(step.arguments.items(), key=lambda item: str(item[0]))
                },
            }
            encoded = json.dumps(safe_payload, sort_keys=True, separators=(",", ":")).encode()
            diagnostic_key = "cand_invalid_" + hashlib.sha256(encoded).hexdigest()[:16]
        return CandidateRejection(
            target_node_id=str(draft.target_node_ids[0]) if draft.target_node_ids else "",
            capability=step.capability,
            operation=step.operation,
            tool_id=str(step.tool_id),
            reason=reason,
            field=field,
            candidate_key=diagnostic_key,
        )

    def _resolve_tool_with_reason(
        self, plan_step: PlanStep
    ) -> tuple[ToolDescriptor | None, tuple[str, str] | None]:
        """The tool to run, or ``(None, (reason, field))`` explaining why none was usable."""
        preferred, capability, operation = (
            plan_step.tool_id,
            plan_step.capability,
            plan_step.operation,
        )
        if not tool_matches_action_namespace(preferred, capability):
            return None, ("tool_outside_capability_namespace", "")
        if self._catalog is None:
            # No live Registry: the provider's declared binding is the only option.
            return (
                ToolDescriptor(
                    tool_id=preferred,
                    server_id="legacy-playbook",
                    tool_name=str(preferred).rpartition(".")[2] or str(preferred),
                    capability_tags=[capability],
                    risk_level=RiskLevel.LOW,
                    health_status=ToolHealthStatus.UNKNOWN,
                ),
                None,
            )

        action_tools = self._catalog.list_tools_for_action(capability, operation)
        if not action_tools:
            # "Nothing in this namespace at all" and "the namespace exists
            # but no tool is bound to this intent" are different operator
            # problems: the first is a missing capability, the second an
            # unsupported operation. Collapsing them into one reason hides
            # which configuration is actually missing.
            capability_tools = self._catalog.list_by_capability(capability)
            if not capability_tools:
                return None, ("capability_unavailable", "")
            # list_tools_for_action hides unhealthy tools, so an
            # all-unhealthy namespace presents as "nothing serves this
            # action". Reporting that as an unsupported operation would send
            # the operator looking for a binding that is already there.
            if all(
                tool.health_status is ToolHealthStatus.UNHEALTHY
                for tool in capability_tools
            ):
                return None, ("tool_unhealthy", "")
            return None, ("operation_unsupported", "")
        usable = [
            tool
            for tool in action_tools
            if tool.health_status is not ToolHealthStatus.UNHEALTHY
            and (
                not tool.target_types
                or plan_step.resolved_target().kind in tool.target_types
            )
        ]
        if not usable:
            return None, ("target_type_mismatch", "")
        healthy = [
            tool
            for tool in usable
            if not plan_step.compatible_tool_ids or tool.tool_id in plan_step.compatible_tool_ids
        ]
        if not healthy:
            return None, ("no_healthy_compatible_tool", "")
        schema_ok = [
            tool
            for tool in healthy
            if self._arguments_match_schema(
                self._bind_target_arguments(
                    plan_step.arguments, plan_step.resolved_target(), tool, plan_step.operation
                ),
                tool.input_schema,
            )
        ]
        if not schema_ok:
            bound_arguments = self._bind_target_arguments(
                plan_step.arguments,
                plan_step.resolved_target(),
                healthy[0],
                plan_step.operation,
            )
            reason, field = self._schema_rejection(
                bound_arguments, healthy[0].input_schema
            ) or ("schema_mismatch", "")
            return None, (reason, field)
        return select_tool(schema_ok), None

    @staticmethod
    def _bind_target_arguments(
        arguments: dict,
        target: AnyTarget,
        tool: ToolDescriptor,
        operation: str,
    ) -> dict:
        """Fill schema-declared identity fields from the selected target.

        Only binds what the selected target actually knows: target identity
        (host/target_ref and friends) and an actually known endpoint path
        (concrete Endpoint/URL path). Never invents values. Explicit
        arguments always win; when no known path exists the argument stays
        missing so schema validation reports it as missing for the LLM
        to refill.
        """

        values = dict(arguments)
        target_values: dict[str, object] = {}
        if isinstance(target, HostTarget):
            target_values.update(host=target.host, target_ref=target.host)
        elif isinstance(target, DomainTarget):
            target_values.update(host=target.domain, target_ref=target.domain)
        elif isinstance(target, UrlTarget):
            target_values.update(url=target.url, target_ref=target.url)
        elif isinstance(target, EndpointTarget):
            target_values.update(
                host=target.host,
                target_ref=target.host,
                port=target.port,
                path=target.path,
            )
        elif isinstance(target, ServiceTarget):
            target_values.update(
                service=target.service_name,
                target_ref=target.host or target.service_name,
                host=target.host,
            )
        elif isinstance(target, AssetTarget):
            target_values.update(
                asset_ref=str(target.asset_id),
                apk_ref=str(target.asset_id),
                artifact_ref=str(target.asset_id),
            )

        properties = tool.input_schema.get("properties", {})
        if not isinstance(properties, dict):
            return values
        for name, value in target_values.items():
            if name in properties and value is not None and name not in values:
                values[name] = value
        if (
            operation == "endpoint_discovery"
            and "path" in tool.required_arguments
            and "path" not in values
        ):
            known_path = None
            if isinstance(target, UrlTarget):
                known_path = urlsplit(target.url).path or None
            # EndpointTarget.path is already bound above via target_values.
            # Bind here only when the target actually knows a path; otherwise
            # leave it missing so _schema_rejection reports
            # missing_required_argument for the LLM to refill.
            if known_path:
                values["path"] = known_path
        return values

    @staticmethod
    def _schema_rejection(arguments: dict, schema: dict) -> tuple[str, str] | None:
        """The first reason ``arguments`` fail ``schema`` as ``(reason, field)``, or ``None``."""
        if not isinstance(schema, dict):
            return ("no_input_schema", "")
        try:
            json.dumps(arguments, sort_keys=True, separators=(",", ":"))
            schema_blob = json.dumps(schema, sort_keys=True, separators=(",", ":"))
            validator = _compiled_validator(schema_blob)
        except (SchemaError, TypeError, ValueError):
            return ("invalid_tool_schema", "")
        errors = sorted(validator.iter_errors(arguments), key=lambda e: list(e.absolute_path))
        if not errors:
            return None
        error = errors[0]
        if error.validator == "required":
            required = error.validator_value if isinstance(error.validator_value, list) else []
            missing = next((name for name in required if name not in (arguments or {})), "")
            return ("missing_required_argument", str(missing))
        return ("schema_mismatch", ".".join(str(part) for part in error.absolute_path))

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
        "prerequisite_ids": sorted(draft.prerequisite_ids),
        "hypothesis": hypothesis.description if hypothesis else None,
        "existing_hypothesis_id": str(draft.plan_step.existing_hypothesis_id or ""),
        "prerequisite_fingerprint": draft.prerequisite_fingerprint,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return "cand_" + hashlib.sha256(encoded).hexdigest()[:24]
