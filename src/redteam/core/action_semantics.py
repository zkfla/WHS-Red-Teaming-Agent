"""Shared intent vocabulary; names are not tool bindings or permission grants.

Capability validates operator-approved bindings against this contract, and
Orchestration validates proposed actions against the very same contract.
"""

from __future__ import annotations

import re

CANONICAL_CAPABILITIES = frozenset({"recon", "web", "mobile", "analysis"})
CANONICAL_OPERATIONS = frozenset(
    {
        "port_scan",
        "service_discovery",
        "endpoint_discovery",
        "http_request",
        "auth_attempt",
        "parameter_fuzz",
        "file_upload",
        "app_launch",
        "device_shell",
        "runtime_instrumentation",
        "static_analysis",
        "response_analysis",
        "directory_list",
        "file_read",
        "file_search",
        "file_write",
        "file_edit",
        "command_run",
        "command_job_start",
        "command_job_status",
        "command_job_cancel",
        "page_read",
        "web_search",
    }
)
_LOWER_SNAKE_CASE = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")


class ActionNamingError(ValueError):
    """A stable, non-target rejection reason shared across team boundaries."""

    reason = "invalid_action_name"
    field = ""


class UnsupportedCapabilityError(ActionNamingError):
    reason = "capability_unsupported"
    field = "capability"


class InvalidOperationFormatError(ActionNamingError):
    reason = "operation_invalid_format"
    field = "operation"


class UnsupportedOperationError(ActionNamingError):
    reason = "operation_unsupported"
    field = "operation"


def validate_capability_name(capability: str) -> None:
    if capability not in CANONICAL_CAPABILITIES:
        raise UnsupportedCapabilityError(
            f"capability {capability!r} is not a canonical v1 namespace"
        )


def validate_operation_format(operation: str) -> None:
    if not isinstance(operation, str) or not _LOWER_SNAKE_CASE.fullmatch(operation):
        raise InvalidOperationFormatError(f"operation {operation!r} must be lower_snake_case")


def validate_operation_name(operation: str) -> None:
    validate_operation_format(operation)
    if operation not in CANONICAL_OPERATIONS:
        raise UnsupportedOperationError(
            f"operation {operation!r} is not in the canonical v1 contract"
        )


def canonical_action_key(capability: str, operation: str) -> str:
    """Validate an intent, independently of any ToolId or ROE decision."""
    validate_capability_name(capability)
    validate_operation_name(operation)
    return f"{capability}.{operation}"
