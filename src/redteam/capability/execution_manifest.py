"""Pinned manifest for the proposed execution-substrate MCP operations."""

from __future__ import annotations

from dataclasses import dataclass

from redteam.capability.servers.execution import CommandResult
from redteam.core.contracts.tool import ToolDescriptor
from redteam.core.enums import RiskLevel


@dataclass(frozen=True)
class ExecutionToolSpec:
    operation: str
    required: frozenset[str]
    optional: frozenset[str]
    output_schema: dict
    risk_level: RiskLevel


_CONTEXT = frozenset({"engagement_id", "run_id"})
EXECUTION_TOOLS = {
    "run_command": ExecutionToolSpec(
        operation="command_run",
        required=frozenset({"asset_ref", "executable", "args"}),
        optional=_CONTEXT | {"timeout_seconds"},
        output_schema=CommandResult.model_json_schema(),
        risk_level=RiskLevel.HIGH,
    ),
}


def minimum_execution_risk() -> RiskLevel:
    return RiskLevel.HIGH


def validate_execution_descriptor(descriptor: ToolDescriptor, asset_ref: str) -> None:
    spec = EXECUTION_TOOLS.get(descriptor.tool_name)
    if spec is None or descriptor.capability_tags != ["analysis"]:
        raise ValueError("execution discovery contains unexpected tool/capability")
    if (
        set(descriptor.required_arguments) != spec.required
        or set(descriptor.optional_arguments) != spec.optional
        or descriptor.operations != [spec.operation]
        or descriptor.target_types != ["asset"]
        or descriptor.output_schema != spec.output_schema
    ):
        raise ValueError("execution discovery schema/operation differs from pinned manifest")

    properties = descriptor.input_schema.get("properties", {})
    if properties.get("asset_ref", {}).get("const") != asset_ref:
        raise ValueError("execution discovery asset_ref differs from operator configuration")
    expected_types = {
        "asset_ref": "string",
        "executable": "string",
        "args": "array",
        "timeout_seconds": "number",
        "engagement_id": "string",
        "run_id": "string",
    }
    if any(
        properties.get(name, {}).get("type") != expected
        for name, expected in expected_types.items()
    ):
        raise ValueError("execution discovery argument type differs from pinned manifest")
    if properties["args"].get("maxItems") != 64:
        raise ValueError("execution discovery args limit differs from pinned manifest")
