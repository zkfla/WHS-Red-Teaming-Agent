"""Compatibility exports; action meanings are owned by core.action_semantics."""

from redteam.core.action_semantics import (
    CANONICAL_CAPABILITIES,
    CANONICAL_OPERATIONS,
    ActionNamingError,
    InvalidOperationFormatError,
    UnsupportedCapabilityError,
    UnsupportedOperationError,
    canonical_action_key,
    validate_capability_name,
    validate_operation_format,
    validate_operation_name,
)

__all__ = [
    "CANONICAL_CAPABILITIES",
    "CANONICAL_OPERATIONS",
    "ActionNamingError",
    "InvalidOperationFormatError",
    "UnsupportedCapabilityError",
    "UnsupportedOperationError",
    "canonical_action_key",
    "validate_capability_name",
    "validate_operation_format",
    "validate_operation_name",
]
