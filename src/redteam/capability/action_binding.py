"""Provider-owned action/target binding and retry data.

The web_search / endpoint_discovery / page_read specials previously lived
as inline branches in the candidate compiler and the network-argument
checker. They are data here; central consumers ask generic predicates.
Retry safety is a provider predicate over (capability, operation,
arguments); the execute node keeps its own gates (retryable error, LOW
risk, bounded attempts, no long-job adapter) and only swaps the
predicate source.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from typing import Any

from redteam.core.provider_contracts import (
    LEGACY_OPERATION_BINDINGS,
    RetryPolicy,
)

BUILTIN_OPERATION_BINDINGS = LEGACY_OPERATION_BINDINGS


def _http_request_retry_safe(arguments: Mapping[str, Any], policy: RetryPolicy) -> bool:
    method = arguments.get("method")
    return (
        isinstance(method, str)
        and method.upper() in policy.http_safe_methods
        and not any(key in arguments for key in policy.http_mutating_argument_keys)
    )


def _builtin_http_request_predicate(arguments: Mapping[str, Any]) -> bool:
    return _http_request_retry_safe(arguments, BUILTIN_RETRY_POLICY)


BUILTIN_RETRY_POLICY = RetryPolicy(
    safe_actions=frozenset(
        {
            "recon.service_discovery",
            "recon.endpoint_discovery",
            "analysis.response_analysis",
            "mobile.static_analysis",
        }
    ),
    http_safe_methods=frozenset({"GET", "HEAD"}),
    http_mutating_argument_keys=frozenset({"body", "data", "json", "payload"}),
    conditional_predicates=(("web.http_request", _builtin_http_request_predicate),),
)


def declares_retry_safe(
    namespace: str,
    operations: Collection[str],
    *,
    policy: RetryPolicy | None = None,
) -> bool:
    active = BUILTIN_RETRY_POLICY if policy is None else policy
    bound = list(operations)
    if not bound:
        return False
    return all((namespace + "." + str(operation)) in active.safe_actions for operation in bound)


KIND_ACTION_BINDING = "capability.action_binding"
KIND_RETRY_POLICY = "capability.retry_policy"


def extension_contributions() -> tuple:
    """Action binding rows plus the retry policy as contributions."""

    from redteam.core.extensions import Contribution, value_factory

    contributions = [
        Contribution(
            kind=KIND_ACTION_BINDING,
            contribution_id="operation:" + spec.operation,
            version="1",
            factory=value_factory(spec),
        )
        for spec in BUILTIN_OPERATION_BINDINGS
    ]
    contributions.append(
        Contribution(
            kind=KIND_RETRY_POLICY,
            contribution_id="default",
            version="1",
            factory=lambda _context: BUILTIN_RETRY_POLICY,
        )
    )
    return tuple(contributions)
