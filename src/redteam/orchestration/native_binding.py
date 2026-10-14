"""Resolve native tool input using registered bindings and current targets.

No investigation plan is created here. Control still owns permission;
matching an input to a supplied address never asserts an observation.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any
from urllib.parse import urlsplit

from redteam.attack_graph.identity import node_id_for_target, target_for_node
from redteam.attack_graph.predicates import facts_of
from redteam.core.contracts.target import AnyTarget, EndpointTarget, UrlTarget
from redteam.core.ids import GraphNodeId
from redteam.orchestration.knowledge_validation import InternalAssessment
from redteam.orchestration.target_binding import network_identity


def exposed_tool_name(tool_id: str) -> str:
    """Provider-compatible name derived from one registered identity."""
    clean = re.sub(r"[^A-Za-z0-9_-]", "_", tool_id)
    suffix = hashlib.sha256(tool_id.encode()).hexdigest()[:10]
    return clean[:50] + "_" + suffix


def _execution_target_key(target: AnyTarget) -> str:
    # Graph identities may normalize concrete resource paths. Deduplicate
    # only equal execution addresses, ignoring an attached graph reference.
    return json.dumps(target.model_dump(mode="json", exclude={"graph_node_id"}), sort_keys=True)


async def resolve_native_target(
    deps: Any, tool: Any, arguments: dict, material: InternalAssessment
):
    if not tool.requires_target:
        return None
    from redteam.attack_graph.service import AttackGraphService
    from redteam.orchestration.engine import (
        DecisionRuleError,
        asset_argument_bindings,
        target_aliases,
        validate_argument_target,
    )

    service = AttackGraphService(deps.graph_store)
    candidates: dict[str, AnyTarget] = {}
    for identity, reference in material.targets.items():
        target = reference.execution_target
        if target is None:
            node = await service.get_node(GraphNodeId(identity))
            if node is None:
                continue
            observed = facts_of(node.properties)
            try:
                target = target_for_node(
                    node.id,
                    node.type,
                    observed=observed.get("observed_target"),
                    device_serial=observed.get("device_serial"),
                    device_ref=observed.get("device_ref"),
                    package_name=observed.get("package_name"),
                )
            except (TypeError, ValueError):
                continue
        candidates[_execution_target_key(target)] = target

    # Explicit URL inputs may choose an unvisited path on a supplied address.
    # This is an execution target, not a measured endpoint or scope grant.
    url_fields = {"url", "base_url", "target_url"} | {
        remote
        for remote, common in tool.argument_bindings.items()
        if common in {"url", "base_url", "target_url"}
    }
    for field in url_fields:
        value = arguments.get(field)
        if not isinstance(value, str) or urlsplit(value).scheme not in {"http", "https"}:
            continue
        target = UrlTarget(url=value)
        address_identity = network_identity(target)
        if any(
            (known := network_identity(existing)) is not None
            and address_identity is not None
            and known.host == address_identity.host
            and (known.port is None or known.port == address_identity.port)
            for existing in candidates.values()
        ):
            candidates[_execution_target_key(target)] = target

    matches = []
    target_ref_rejection: DecisionRuleError | None = None
    for target in candidates.values():
        if tool.target_types and target.kind not in tool.target_types:
            continue
        try:
            validate_argument_target(
                target,
                arguments,
                target_aliases(deps, target),
                asset_bindings=asset_argument_bindings(deps, target),
                operation_bindings=deps.operation_bindings,
                argument_bindings=tool.argument_bindings,
                target_binding=tool.target_binding,
            )
        except DecisionRuleError as exc:
            if exc.diagnostic.code == "target_ref_source_mismatch":
                target_ref_rejection = exc
            continue
        except (TypeError, ValueError):
            continue
        matches.append(target)
    # A concrete URL requested by the caller is more specific than its origin.
    explicit_urls = {
        arguments.get(field) for field in url_fields if isinstance(arguments.get(field), str)
    }
    exact = [
        target
        for target in matches
        if isinstance(target, UrlTarget) and target.url in explicit_urls
    ]
    if len(exact) == 1:
        return exact[0]
    path_fields = {"path"} | {
        remote for remote, common in tool.argument_bindings.items() if common == "path"
    }
    paths = {
        arguments[field] for field in path_fields if isinstance(arguments.get(field), str)
    }
    exact_paths = [
        target for target in matches
        if isinstance(target, EndpointTarget) and target.path in paths
    ]
    if len(exact_paths) == 1:
        return exact_paths[0]
    if len(matches) == 1:
        return matches[0]
    if not matches and target_ref_rejection is not None:
        raise target_ref_rejection
    raise DecisionRuleError(
        "the tool call must identify exactly one supplied or observed target; "
        "include the target in the tool arguments (for example target_ref or url)",
        code="ambiguous_tool_target",
        location="arguments",
    )


def native_address_known(target: AnyTarget, material: InternalAssessment) -> bool:
    """Known authority suffices for choosing an unvisited URL; never grants scope."""
    if str(node_id_for_target(target)) in material.targets:
        return True
    identity = network_identity(target)
    if identity is None:
        return False
    for reference in material.targets.values():
        address = reference.address
        if not address:
            continue
        parsed = urlsplit(address if "://" in address else "//" + address)
        if parsed.username is not None or parsed.password is not None:
            continue
        if parsed.hostname != identity.host:
            continue
        port = parsed.port
        if port is None and parsed.scheme in {"http", "https"}:
            port = 443 if parsed.scheme == "https" else 80
        if port is None or port == identity.port:
            return True
    return False
