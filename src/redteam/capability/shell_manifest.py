"""Pinned manifest for the builtin shell-workroom tools. Owned by Capability Team.

TASK-003: four tools behind one in-process adapter (shell_run plus
shell_job_start / shell_job_status / shell_job_cancel). The operations
are intentionally distinct from the execution MCP command_* operations
so risk and policy bindings for the allowlisted one-shot engine do not
leak onto arbitrary shell scripts and vice versa. Starting shell work
is MEDIUM: the execution itself is bounded (network-none, non-root,
capped script/output/time bounds), so ambiguity alone never forces
approval -- approval fires only through explicit operator rules or
matching command-pattern rules. Polling a job is LOW and cancelling one
is MEDIUM. Shell results never imply facts, findings, or
goal completion; see TASK-004A for the evidence-to-graph recording path.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from redteam.capability.shell_policy import WORKROOM_KEY_PATTERN, ShellWorkroomPolicy
from redteam.core.contracts.tool import ToolDescriptor, ToolTargetBinding
from redteam.core.enums import RiskLevel
from redteam.core.ids import ToolId

SHELL_SERVER_ID = "shell-workroom"
SHELL_NAMESPACE = "analysis"

RUN_TOOL_NAME = "shell_run"
JOB_START_TOOL_NAME = "shell_job_start"
JOB_STATUS_TOOL_NAME = "shell_job_status"
JOB_CANCEL_TOOL_NAME = "shell_job_cancel"

RUN_OPERATION = "shell_run"
JOB_START_OPERATION = "shell_job_start"
JOB_STATUS_OPERATION = "shell_job_status"
JOB_CANCEL_OPERATION = "shell_job_cancel"

SHELL_LANGUAGES = ("bash", "python3")

def _timeout_maximum(policy: ShellWorkroomPolicy | None) -> float:
    """Active policy bound, else the policy model default (no drift)."""
    if policy is not None:
        return float(policy.max_timeout_seconds)
    return float(ShellWorkroomPolicy.model_fields["max_timeout_seconds"].default)


def _script_max_chars(policy: ShellWorkroomPolicy | None) -> int:
    """Active policy bound, else the policy model default (no drift)."""
    if policy is not None:
        return int(policy.max_script_bytes)
    return int(ShellWorkroomPolicy.model_fields["max_script_bytes"].default)


def _run_input_schema(policy: ShellWorkroomPolicy | None) -> dict:
    """Script-execution input schema with policy-derived bounds.

    ``maxLength`` counts JSON characters while the runtime enforces UTF-8
    bytes; the description says so because the schema language cannot
    express a byte limit.
    """
    timeout_max = _timeout_maximum(policy)
    script_max = _script_max_chars(policy)
    return {
        "type": "object",
        "properties": {
            "workroom_key": {
                "type": "string",
                "readOnly": True,
                "minLength": 1,
                "maxLength": 128,
                "pattern": WORKROOM_KEY_PATTERN,
                "description": (
                    "Server-bound execution identity. Omit it when calling "
                    "through the gateway: the provider binds the trusted "
                    "workroom key derived from the execution identity "
                    "(run_id plus instance_id) and rejects any mismatched "
                    "caller-supplied value. Supply it only already bound; "
                    "the value must equal the trusted key."
                ),
            },
            "language": {"type": "string", "enum": ["bash", "python3"]},
            "script": {
                "type": "string",
                "minLength": 1,
                "maxLength": script_max,
                "description": (
                    "Script source fed to the interpreter on stdin. The schema " +
                    "counts characters; the runtime additionally rejects " +
                    f"scripts over {script_max} UTF-8 bytes."
                ),
            },
            "timeout_seconds": {
                "type": "number",
                "exclusiveMinimum": 0,
                "maximum": timeout_max,
                "description": (
                    "Per-execution deadline in seconds " +
                    f"(0 < t <= {timeout_max}). Expiry is reported via " +
                    "timed_out with a null exit_code."
                ),
            },
        },
        "required": ["language", "script"],
        "additionalProperties": False,
    }


def _job_input_schema() -> dict:
    """Job poll/cancel input schema (the key pattern is policy-independent)."""
    return {
        "type": "object",
        "properties": {
            "workroom_key": {
                "type": "string",
                "readOnly": True,
                "minLength": 1,
                "maxLength": 128,
                "pattern": WORKROOM_KEY_PATTERN,
                "description": (
                    "Server-bound execution identity. Omit it when calling "
                    "through the gateway: the provider binds the trusted "
                    "workroom key derived from the execution identity "
                    "(run_id plus instance_id) and rejects any mismatched "
                    "caller-supplied value. Supply it only already bound; "
                    "the value must equal the trusted key."
                ),
            },
            "job_id": {"type": "string", "minLength": 1, "maxLength": 128},
        },
        "required": ["job_id"],
        "additionalProperties": False,
    }


RUN_INPUT_SCHEMA = _run_input_schema(None)
JOB_STATUS_INPUT_SCHEMA = _job_input_schema()

RUN_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "exit_code": {"type": ["integer", "null"]},
        "stdout": {"type": "string"},
        "stderr": {"type": "string"},
        "timed_out": {"type": "boolean"},
        "truncated": {"type": "boolean"},
        "duration_ms": {"type": "integer", "minimum": 0},
        "script_sha256": {"type": "string"},
        "workroom_key": {"type": "string"},
    },
    "required": [
        "exit_code", "stdout", "stderr", "timed_out",
        "truncated", "duration_ms", "script_sha256", "workroom_key",
    ],
    "additionalProperties": False,
}

JOB_START_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "job_id": {"type": "string"},
        "status": {"type": "string", "const": "running"},
        "workroom_key": {"type": "string"},
    },
    "required": ["job_id", "status", "workroom_key"],
    "additionalProperties": False,
}

JOB_STATUS_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "job_id": {"type": "string"},
        "status": {"type": "string"},
        "result": {"type": ["object", "null"]},
        "error": {"type": ["string", "null"]},
        "workroom_key": {"type": "string"},
    },
    "required": ["job_id", "status", "result", "error", "workroom_key"],
    "additionalProperties": False,
}

JOB_CANCEL_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "job_id": {"type": "string"},
        "status": {"type": "string"},
        "cancelled": {"type": "boolean"},
        "workroom_key": {"type": "string"},
    },
    "required": ["job_id", "status", "cancelled", "workroom_key"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class ShellToolSpec:
    tool_name: str
    operation: str
    input_schema: dict
    output_schema: dict
    risk_level: RiskLevel
    command_fields: list = field(default_factory=list)
    description: str = ""


_RUN_DESCRIPTION = (
    "Stateful shell workroom (shell_run). Network closed. Output streams are " +
    "capped at the policy max_output_bytes during collection " +
    "(truncated marks a reached cap); the gateway applies its own " +
    "per-server serialized-output cut afterwards."
)
_JOB_START_DESCRIPTION = (
    "Stateful shell workroom (shell_job_start). Network closed. Starts one " +
    "tagged background execution; poll it with shell_job_status."
)
_JOB_STATUS_DESCRIPTION = (
    "Stateful shell workroom (shell_job_status). Network closed. Jobs live " +
    "in adapter process memory: after a restart every old job_id is " +
    "permanently unknown (shell_unknown_job); recovery belongs to TASK-007."
)
_JOB_CANCEL_DESCRIPTION = (
    "Stateful shell workroom (shell_job_cancel). Network closed. Cancel " +
    "stops the waiter, reaps the host docker-exec child, and terminates " +
    "through the root supervisor as the single deadline and termination " +
    "authority (TERM, grace, KILL, reap with subreaper tracking). A " +
    "verified teardown reclaims its max_jobs slot immediately; a " +
    "teardown-unverified cancel holds cleanup-unknown (slot held) until a " +
    "supervisor-verified outcome or an authoritative container teardown " +
    "settles it. TTL and deadlines alone never release a possibly-live " +
    "budget."
)


SHELL_TOOLS = {
    RUN_TOOL_NAME: ShellToolSpec(
        tool_name=RUN_TOOL_NAME,
        operation=RUN_OPERATION,
        input_schema=RUN_INPUT_SCHEMA,
        output_schema=RUN_OUTPUT_SCHEMA,
        risk_level=RiskLevel.MEDIUM,
        command_fields=["language", "script"],
        description=_RUN_DESCRIPTION,
    ),
    JOB_START_TOOL_NAME: ShellToolSpec(
        tool_name=JOB_START_TOOL_NAME,
        operation=JOB_START_OPERATION,
        input_schema=RUN_INPUT_SCHEMA,
        output_schema=JOB_START_OUTPUT_SCHEMA,
        risk_level=RiskLevel.MEDIUM,
        command_fields=["language", "script"],
        description=_JOB_START_DESCRIPTION,
    ),
    JOB_STATUS_TOOL_NAME: ShellToolSpec(
        tool_name=JOB_STATUS_TOOL_NAME,
        operation=JOB_STATUS_OPERATION,
        input_schema=JOB_STATUS_INPUT_SCHEMA,
        output_schema=JOB_STATUS_OUTPUT_SCHEMA,
        risk_level=RiskLevel.LOW,
        description=_JOB_STATUS_DESCRIPTION,
    ),
    JOB_CANCEL_TOOL_NAME: ShellToolSpec(
        tool_name=JOB_CANCEL_TOOL_NAME,
        operation=JOB_CANCEL_OPERATION,
        input_schema=JOB_STATUS_INPUT_SCHEMA,
        output_schema=JOB_CANCEL_OUTPUT_SCHEMA,
        risk_level=RiskLevel.MEDIUM,
        description=_JOB_CANCEL_DESCRIPTION,
    ),
}


def _descriptor_for(spec: ShellToolSpec, input_schema: dict) -> ToolDescriptor:
    return ToolDescriptor(
        tool_id=ToolId(SHELL_NAMESPACE + "." + spec.tool_name),
        server_id=SHELL_SERVER_ID,
        tool_name=spec.tool_name,
        description=(
            spec.description
            or "Stateful shell workroom (" + spec.tool_name + "). Network closed."
        ),
        input_schema=dict(input_schema),
        output_schema=dict(spec.output_schema),
        operations=[spec.operation],
        target_types=[],
        capability_tags=[SHELL_NAMESPACE],
        risk_level=spec.risk_level,
        retry_safe=False,
        argument_bindings={},
        outbound_text_fields=[],
        target_binding=ToolTargetBinding(binds_target_arguments=False),
        requires_target=False,
        command_fields=list(spec.command_fields),
        availability_impact=False,
    )


def build_shell_descriptors(policy: ShellWorkroomPolicy | None = None) -> list:
    """Return the four pinned shell-workroom descriptors.

    Timeout/script bounds come from the active policy so the schema never
    advertises values the runtime would reject; ``None`` falls back to the
    policy model defaults.
    """
    run_schema = _run_input_schema(policy)
    job_schema = _job_input_schema()
    schemas = {
        RUN_TOOL_NAME: run_schema,
        JOB_START_TOOL_NAME: run_schema,
        JOB_STATUS_TOOL_NAME: job_schema,
        JOB_CANCEL_TOOL_NAME: job_schema,
    }
    return [
        _descriptor_for(SHELL_TOOLS[name], schemas[name])
        for name in (
            RUN_TOOL_NAME,
            JOB_START_TOOL_NAME,
            JOB_STATUS_TOOL_NAME,
            JOB_CANCEL_TOOL_NAME,
        )
    ]


def _check_key_pattern(descriptor) -> None:
    """The workroom-key binding contract stays pinned.

    The pattern is policy-independent, and the key is never required:
    the gateway-side binder injects the trusted key, so a required key
    would force the model to guess an identity it must not supply.
    """
    try:
        prop = descriptor.input_schema["properties"]["workroom_key"]
    except (KeyError, TypeError, AttributeError):
        raise ValueError("shell descriptor is missing the workroom_key property") from None
    if not isinstance(prop, dict) or prop.get("pattern") != WORKROOM_KEY_PATTERN:
        raise ValueError("shell descriptor carries an unexpected workroom_key pattern")
    if prop.get("readOnly") is not True:
        raise ValueError("shell descriptor must keep workroom_key server-bound")
    required = descriptor.input_schema.get("required", [])
    if "workroom_key" in required:
        raise ValueError("shell descriptor must not require workroom_key")


def _check_derived_bounds(descriptors: list, policy: ShellWorkroomPolicy) -> None:
    """Policy-derived fields are verified by recomputation, not by pinning."""
    reference = {d.tool_name: d for d in build_shell_descriptors(policy)}
    for descriptor in descriptors:
        expected = reference[descriptor.tool_name].input_schema.get("properties", {})
        actual = descriptor.input_schema.get("properties", {})
        for prop_name, subfield in (("timeout_seconds", "maximum"), ("script", "maxLength")):
            if prop_name in expected:
                if actual.get(prop_name, {}).get(subfield) != expected[prop_name][subfield]:
                    raise ValueError("shell descriptor bounds differ from the active policy")


def validate_shell_descriptors(
    descriptors: list, policy: ShellWorkroomPolicy | None = None
) -> None:
    """Run the shared discovery-boundary validation over shell descriptors.

    The pin covers identity, operations, risk, command fields, output
    schemas, and the policy-independent key pattern. Timeout/script bounds
    are policy-derived: excluded from the pin and, when ``policy`` is given,
    verified by recomputation instead.
    """
    from redteam.capability.manifest import validate_tool_manifest

    if len(descriptors) != len(SHELL_TOOLS):
        raise ValueError("shell catalog must expose exactly four tools")
    seen = set()
    for descriptor in descriptors:
        validate_tool_manifest(descriptor)
        if descriptor.server_id != SHELL_SERVER_ID:
            raise ValueError("shell descriptor carries an unexpected server_id")
        if descriptor.tool_name in seen:
            raise ValueError("duplicate shell tool name")
        seen.add(descriptor.tool_name)
        spec = SHELL_TOOLS.get(descriptor.tool_name)
        if spec is None:
            raise ValueError("shell descriptor is not part of the pinned manifest")
        if (
            descriptor.operations != [spec.operation]
            or descriptor.risk_level != spec.risk_level
            or descriptor.command_fields != spec.command_fields
            or descriptor.output_schema != spec.output_schema
        ):
            raise ValueError("shell descriptor differs from the pinned manifest")
        _check_key_pattern(descriptor)
    if seen != set(SHELL_TOOLS):
        raise ValueError("shell catalog is missing pinned tools")
    if policy is not None:
        _check_derived_bounds(descriptors, policy)
