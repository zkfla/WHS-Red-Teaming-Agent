"""Legacy playbook -> validated, tool-bound, deterministically ranked candidates."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError

from redteam.attack_graph.identity import node_id_for_target
from redteam.attack_graph.observation import GraphObservation
from redteam.attack_graph.view import AttackGraphView
from redteam.core.action_semantics import ActionNamingError
from redteam.core.contracts.target import (
    AnyTarget,
    AssetTarget,
    DeviceTarget,
    DomainTarget,
    EndpointTarget,
    HostTarget,
    ServiceTarget,
    UrlTarget,
)
from redteam.core.contracts.tool import ToolDescriptor
from redteam.core.enums import RiskLevel, ToolHealthStatus
from redteam.core.provider_contracts import operation_target_binding
from redteam.core.tool_binding import SELECTED_TOOL
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
from redteam.orchestration.target_binding import network_identity, validate_network_arguments

if TYPE_CHECKING:
    from redteam.core.provider_contracts import OperationTargetBinding


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

    def __init__(
        self,
        catalog: ToolCatalog | None = None,
        *,
        operation_bindings: Mapping[str, OperationTargetBinding] | None = None,
    ) -> None:
        self._catalog = catalog
        self._operation_bindings = operation_bindings

    async def compile_playbook(
        self,
        spec: AgentSpec,
        view: AttackGraphView,
        observation: GraphObservation,
        *,
        step: int,
    ) -> list[ExecutableCandidate]:
        drafts = await PlaybookCandidateProvider().propose(spec, view, observation, step=step)
        allowed = set(spec.capabilities) if spec.capabilities else None
        return self.compile_drafts(drafts, allowed_capabilities=allowed)

    def compile_drafts(
        self,
        drafts: list[CandidateDraft],
        *,
        allowed_capabilities: set[str] | None = None,
    ) -> list[ExecutableCandidate]:
        """Apply generic catalog/schema/health constraints to provider drafts."""
        return self.compile_drafts_with_rejections(
            drafts, allowed_capabilities=allowed_capabilities
        )[0]

    def compile_drafts_with_rejections(
        self,
        drafts: list[CandidateDraft],
        *,
        allowed_capabilities: set[str] | None = None,
    ) -> tuple[list[ExecutableCandidate], list[CandidateRejection]]:
        """Like :meth:`compile_drafts`, but also returns why each dropped draft
        was dropped (dedup drops are not reported)."""
        candidates: list[ExecutableCandidate] = []
        rejections: list[CandidateRejection] = []
        seen: set[str] = set()
        for draft in drafts:
            candidate, rejection = self._evaluate_draft(
                draft, seen, allowed_capabilities=allowed_capabilities
            )
            if candidate is not None:
                candidates.append(candidate)
            elif rejection is not None:
                rejections.append(rejection)
        return (
            sorted(candidates, key=lambda item: (-item.priority.total, item.candidate_key)),
            rejections,
        )

    def _evaluate_draft(
        self,
        draft: CandidateDraft,
        seen: set[str],
        *,
        allowed_capabilities: set[str] | None,
    ) -> tuple[ExecutableCandidate | None, CandidateRejection | None]:
        plan_step = draft.plan_step
        direct = self._is_direct_step(plan_step)
        if not direct and allowed_capabilities is not None:
            if plan_step.capability not in allowed_capabilities:
                return None, self._reject(draft, "agent_capability_not_allowed")
        try:
            json.dumps(plan_step.arguments, sort_keys=True, allow_nan=False)
        except (TypeError, ValueError):
            return None, self._reject(draft, "non_json_arguments")
        if not direct:
            try:
                canonical_action_key(plan_step.capability, plan_step.operation)
            except ActionNamingError as exc:
                return None, self._reject(draft, exc.reason, field=exc.field)
        try:
            target = plan_step.resolved_target()
        except ValueError:
            return None, self._reject(draft, "unresolved_target")
        if target is not None and node_id_for_target(target) not in draft.target_node_ids:
            return None, self._reject(draft, "target_mismatch")
        if not direct and target is not None:
            try:
                validate_network_arguments(
                    target,
                    plan_step.arguments,
                    operation=plan_step.operation,
                    bindings=self._operation_bindings,
                )
            except ValueError:
                return None, self._reject(draft, "unresolved_target", field="arguments")
        tool, tool_rejection = self._resolve_tool_with_reason(plan_step)
        if tool is None:
            reason, field = tool_rejection or ("no_compatible_tool", "")
            return None, self._reject(draft, reason, field=field)
        bound_arguments = dict(plan_step.arguments) if target is None else (
            self._bind_target_arguments(
                plan_step.arguments,
                target,
                tool,
                plan_step.operation,
                bindings=self._operation_bindings,
                target_binding=getattr(tool, "target_binding", None) if direct else None,
            )
        )
        try:
            if target is not None:
                validate_network_arguments(
                    target,
                    bound_arguments,
                    operation=plan_step.operation,
                    bindings=self._operation_bindings,
                    argument_bindings=getattr(tool, "argument_bindings", None),
                    target_binding=getattr(tool, "target_binding", None) if direct else None,
                )
        except ValueError:
            return None, self._reject(draft, "unresolved_target", field="arguments")
        bound_step = plan_step.model_copy(
            update={"arguments": bound_arguments, "tool_id": tool.tool_id}
        )
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

    @staticmethod
    def _is_direct_step(plan_step: PlanStep) -> bool:
        return (
            getattr(plan_step, "binding_mode", "legacy_action") == SELECTED_TOOL
            and bool(plan_step.tool_id)
        )

    def _resolve_direct_tool(
        self, plan_step: PlanStep
    ) -> tuple[ToolDescriptor | None, tuple[str, str] | None]:
        if self._catalog is None:
            return None, ("no_compatible_tool", "")
        try:
            tool = self._catalog.get(plan_step.tool_id)
        except ValueError:
            return None, ("no_compatible_tool", "")
        if tool is None:
            return None, ("no_compatible_tool", "")
        if tool.health_status is ToolHealthStatus.UNHEALTHY:
            return None, ("tool_unhealthy", "")
        if plan_step.require_healthy_tool:
            if tool.health_status is not ToolHealthStatus.HEALTHY:
                return None, ("tool_unhealthy", "")
        try:
            target = plan_step.resolved_target()
        except ValueError:
            return None, ("unresolved_target", "")
        if target is None and tool.requires_target:
            return None, ("unresolved_target", "")
        if target is not None and tool.target_types and target.kind not in tool.target_types:
            return None, ("target_type_mismatch", "")
        bound = dict(plan_step.arguments) if target is None else self._bind_target_arguments(
            plan_step.arguments,
            target,
            tool,
            plan_step.operation,
            bindings=self._operation_bindings,
            target_binding=getattr(tool, "target_binding", None),
        )
        if not self._arguments_match_schema(bound, tool.input_schema):
            reason, field = self._schema_rejection(bound, tool.input_schema) or (
                "schema_mismatch",
                "",
            )
            return None, (reason, field)
        return tool, None

    def _resolve_tool_with_reason(
        self, plan_step: PlanStep
    ) -> tuple[ToolDescriptor | None, tuple[str, str] | None]:
        """The tool to run, or ``(None, (reason, field))`` explaining why none was usable."""
        preferred, capability, operation = (
            plan_step.tool_id,
            plan_step.capability,
            plan_step.operation,
        )
        if self._is_direct_step(plan_step):
            return self._resolve_direct_tool(plan_step)
        target = plan_step.resolved_target()
        if target is None:
            return None, ("unresolved_target", "")
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

        try:
            action_tools = self._catalog.list_tools_for_action(capability, operation)
        except ValueError:
            return None, ("no_compatible_tool", "")
        action_tools = [
            tool
            for tool in action_tools
            if capability in tool.capability_tags and operation in tool.operations
        ]
        if not action_tools:
            # The common vocabulary was validated before resolution. An
            # absent namespace is therefore a missing binding, not an
            # unsupported common operation.
            try:
                capability_tools = self._catalog.list_by_capability(capability)
            except ValueError:
                return None, ("no_compatible_tool", "")
            if not capability_tools:
                return None, ("no_tool_for_operation", "operation")
            # list_tools_for_action hides unhealthy tools, so an
            # all-unhealthy namespace presents as "nothing serves this
            # action". Reporting that as a missing binding would send
            # the operator looking for a binding that is already there.
            operation_tools = [tool for tool in capability_tools if operation in tool.operations]
            if operation_tools and all(
                tool.health_status is ToolHealthStatus.UNHEALTHY for tool in operation_tools
            ):
                return None, ("tool_unhealthy", "")
            return None, ("no_tool_for_operation", "operation")
        usable = [
            tool
            for tool in action_tools
            if tool.health_status is not ToolHealthStatus.UNHEALTHY
            and (not tool.target_types or target.kind in tool.target_types)
        ]
        if all(tool.health_status is ToolHealthStatus.UNHEALTHY for tool in action_tools):
            return None, ("tool_unhealthy", "")
        if not usable:
            return None, ("target_type_mismatch", "")
        compatible = set(plan_step.compatible_tool_ids)
        if compatible:
            try:
                compatible = {
                    resolved.tool_id
                    for tool_id in compatible
                    if (resolved := self._catalog.get(tool_id)) is not None
                }
            except ValueError:
                return None, ("no_compatible_tool", "")
        healthy = [
            tool
            for tool in usable
            if (
                not plan_step.require_healthy_tool or tool.health_status is ToolHealthStatus.HEALTHY
            )
            and (not plan_step.compatible_tool_ids or tool.tool_id in compatible)
        ]
        if not healthy:
            return None, ("no_tool_for_operation", "operation")
        try:
            if any(self._catalog.get(tool.tool_id) != tool for tool in healthy):
                return None, ("tool_catalog_inconsistent", "")
        except ValueError:
            return None, ("no_compatible_tool", "")
        schema_ok = [
            tool
            for tool in healthy
            if self._arguments_match_schema(
                self._bind_target_arguments(
                    plan_step.arguments,
                    target,
                    tool,
                    plan_step.operation,
                    bindings=self._operation_bindings,
                ),
                tool.input_schema,
            )
        ]
        if not schema_ok:
            bound_arguments = self._bind_target_arguments(
                plan_step.arguments,
                target,
                healthy[0],
                plan_step.operation,
                bindings=self._operation_bindings,
            )
            reason, field = self._schema_rejection(bound_arguments, healthy[0].input_schema) or (
                "schema_mismatch",
                "",
            )
            return None, (reason, field)
        return select_tool(schema_ok), None

    @staticmethod
    def _bind_target_arguments(
        arguments: dict,
        target: AnyTarget,
        tool: ToolDescriptor,
        operation: str,
        bindings: Mapping[str, OperationTargetBinding] | None = None,
        target_binding: Any | None = None,
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
        elif isinstance(target, DeviceTarget):
            target_values.update(
                device=target.device_ref,
                device_ref=target.device_ref,
                packageName=target.package_name,
                package_name=target.package_name,
                asset_ref=str(target.asset_id or target.package_name),
            )

        properties = tool.input_schema.get("properties", {})
        if not isinstance(properties, dict):
            return values
        declared = tool.argument_bindings
        identity = network_identity(target)
        if identity is not None:
            target_values.update(
                host=identity.host
                if ("port" in properties or "port" in declared.values())
                else identity.authority,
                target_ref=target.url if isinstance(target, UrlTarget) else identity.authority,
                port=identity.port,
            )
            target_values.update(domain=target_values["host"], hostname=target_values["host"])
        if isinstance(target, UrlTarget):
            target_values.update(base_url=target.url, target_url=target.url)
        if target_binding is not None:
            active_binding = target_binding
        else:
            active_binding = operation_target_binding(operation, bindings)
        binds_target_arguments = bool(getattr(active_binding, "binds_target_arguments", True))
        binds_known_path = bool(getattr(active_binding, "binds_known_path_from_url", False))
        if not binds_target_arguments:
            # The TargetRef belongs to the investigation. Search arguments
            # contain the query; a target URL is not a search input.
            target_values = {}
        for name, value in target_values.items():
            if name in properties and value is not None and name not in values:
                values[name] = value
        for input_arg, shared in declared.items():
            if input_arg in values or input_arg not in properties:
                continue
            known = target_values.get(str(shared))
            if known is not None:
                values[input_arg] = known
        if binds_known_path:
            wanted = [
                key
                for key in [
                    "path",
                    *[
                        arg
                        for arg, name in declared.items()
                        if str(name) == "path" and arg != "path"
                    ],
                ]
                if key in tool.required_arguments and key not in values
            ]
            if wanted:
                known_path = None
                if isinstance(target, UrlTarget):
                    known_path = urlsplit(target.url).path or None
                # EndpointTarget.path is already bound above via target_values.
                # Bind here only when the target actually knows a path; otherwise
                # leave it missing so _schema_rejection reports
                # missing_required_argument for the LLM to refill.
                if known_path:
                    for key in wanted:
                        values[key] = known_path
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

    step = draft.plan_step
    if getattr(step, "binding_mode", "legacy_action") == SELECTED_TOOL:
        if step.tool_id:
            direct_payload = {
                "contract_version": draft.contract_version,
                "tool_id": str(step.tool_id),
                "arguments": step.arguments,
                "target_node_ids": sorted(map(str, draft.target_node_ids)),
                "principal_ref": step.principal_ref,
                "prerequisite_fingerprint": draft.prerequisite_fingerprint,
            }
            direct_encoded = json.dumps(direct_payload, sort_keys=True).encode()
            return "cand_" + hashlib.sha256(direct_encoded).hexdigest()[:24]
    hypothesis = draft.plan_step.hypothesis
    payload = {
        "contract_version": draft.contract_version,
        "agent_id": str(draft.agent_id),
        "capability": draft.plan_step.capability,
        "operation": draft.plan_step.operation,
        "attempt_record_key": draft.plan_step.attempt_record_key,
        "arguments": draft.plan_step.arguments,
        "target_node_ids": sorted(map(str, draft.target_node_ids)),
        "prerequisite_ids": sorted(draft.prerequisite_ids),
        "hypothesis": hypothesis.description if hypothesis else None,
        "existing_hypothesis_id": str(draft.plan_step.existing_hypothesis_id or ""),
        "prerequisite_fingerprint": draft.prerequisite_fingerprint,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return "cand_" + hashlib.sha256(encoded).hexdigest()[:24]
