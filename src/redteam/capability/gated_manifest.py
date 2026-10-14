"""Provider-owned gated shell descriptors. Owned by Capability (TASK-005).

Three additive descriptors behind the same in-process shell adapter:
"shell_target_run" (sync targeted run), "shell_target_job_start" (async
targeted start; poll and cancel reuse the existing local tools), and
"shell_install_run" (sync-only package-mirror run). The four local tools
are untouched and stay network-closed.

All three are MEDIUM: the execution is bounded exactly like the local
tools, and explicit operator policy plus ROE govern. "requires_target" is
True and "target_types" is exactly the list the actual resolver supports;
asset, device, and principal kinds are excluded. Selection travels as
"target_ref" (a registry or mirror name) matched by the existing generic
target_ref alias binding, plus an explicit "port" for target runs that is
never defaulted from a registry IP tuple alone. Install runs take a
mirror name only; the reachable port comes from operator configuration.
"""

from __future__ import annotations

from redteam.capability.manifest import validate_tool_manifest
from redteam.capability.shell_manifest import (
    JOB_START_OPERATION,
    JOB_START_OUTPUT_SCHEMA,
    RUN_OPERATION,
    RUN_OUTPUT_SCHEMA,
    SHELL_LANGUAGES,
    SHELL_NAMESPACE,
    SHELL_SERVER_ID,
)
from redteam.capability.shell_policy import WORKROOM_KEY_PATTERN, ShellWorkroomPolicy
from redteam.core.contracts.network import NetworkPurpose
from redteam.core.contracts.tool import ToolDescriptor, ToolTargetBinding
from redteam.core.enums import RiskLevel
from redteam.core.ids import ToolId

SHELL_TARGET_RUN = "shell_target_run"
SHELL_TARGET_JOB_START = "shell_target_job_start"
SHELL_INSTALL_RUN = "shell_install_run"

TARGET_RUN_OPERATION = SHELL_TARGET_RUN
TARGET_JOB_START_OPERATION = SHELL_TARGET_JOB_START
INSTALL_RUN_OPERATION = SHELL_INSTALL_RUN

TARGET_TYPES = ["host", "domain", "url", "endpoint", "service"]

OPERATION_PURPOSE: dict[str, NetworkPurpose] = {
    TARGET_RUN_OPERATION: NetworkPurpose.TARGET,
    TARGET_JOB_START_OPERATION: NetworkPurpose.TARGET,
    INSTALL_RUN_OPERATION: NetworkPurpose.INSTALL,
}

OPERATION_BODY: dict[str, str] = {
    TARGET_RUN_OPERATION: RUN_OPERATION,
    TARGET_JOB_START_OPERATION: JOB_START_OPERATION,
    INSTALL_RUN_OPERATION: RUN_OPERATION,
}

_GATED_TOOL_NAMES = (SHELL_TARGET_RUN, SHELL_TARGET_JOB_START, SHELL_INSTALL_RUN)

_TARGET_RUN_DESCRIPTION = (
    "Stateful shell workroom, gated target route (shell_target_run). "
    "The script runs inside one exclusive per-instance transmit window "
    "that reaches only operator-pinned IPs for the selected registry "
    "target. Enforcement uses pinned IP literals; the registry name is "
    "only a selector. Host/VM loopback is not the workroom loopback. "
    "Scripts have no NET_ADMIN or CAP_NET_RAW; raw-packet modes are "
    "unsupported. Use available unprivileged connection/application modes."
)
_TARGET_JOB_START_DESCRIPTION = (
    "Stateful shell workroom, gated target route (shell_target_job_start). "
    "Starts one tagged background execution inside the exclusive window; "
    "poll it with shell_job_status. Same destination bounds as "
    "shell_target_run. Scripts have no NET_ADMIN or CAP_NET_RAW; "
    "raw-packet modes are unsupported."
)
_INSTALL_RUN_DESCRIPTION = (
    "Stateful shell workroom, gated install route (shell_install_run). "
    "Sync only. Reaches only the operator install proxy for the selected "
    "mirror name; the proxy dials pinned mirror IPs out of band. Never "
    "registers a package as a new tool."
)


def _bounds(policy: ShellWorkroomPolicy | None) -> tuple[float, int]:
    """Timeout and script bounds from the active policy, else its defaults."""
    if policy is not None:
        return float(policy.max_timeout_seconds), int(policy.max_script_bytes)
    fields = ShellWorkroomPolicy.model_fields
    return (
        float(fields["max_timeout_seconds"].default),
        int(fields["max_script_bytes"].default),
    )


def _key_property() -> dict:
    return {
        "type": "string",
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
    }


def _script_properties(timeout_max: float, script_max: int) -> dict:
    return {
        "workroom_key": _key_property(),
        "language": {"type": "string", "enum": list(SHELL_LANGUAGES)},
        "script": {
            "type": "string",
            "minLength": 1,
            "maxLength": script_max,
            "description": (
                "Script source fed to the interpreter on stdin. The schema "
                "counts characters; the runtime additionally rejects "
                "scripts over " + str(script_max) + " UTF-8 bytes."
            ),
        },
        "timeout_seconds": {
            "type": "number",
            "exclusiveMinimum": 0,
            "maximum": timeout_max,
            "description": (
                "Per-execution deadline in seconds "
                "(0 < t <= " + str(timeout_max) + "). Expiry is reported via "
                "timed_out with a null exit_code."
            ),
        },
    }


def _target_ref_property(install: bool) -> dict:
    if install:
        return {
            "type": "string",
            "minLength": 1,
            "maxLength": 256,
            "description": (
                "Package-mirror name from the operator install registry only. "
                "The mirror must also be approved in engagement scope; the "
                "reachable port comes from the operator proxy endpoint, "
                "never from this argument."
            ),
        }
    return {
        "type": "string",
        "minLength": 1,
        "maxLength": 256,
        "description": (
            "Operator registry name for the approved target. Matched "
            "against the approved proposal target by the existing "
            "target_ref alias binding. Enforcement uses only the "
            "operator-pinned IPs for this name, never a caller IP."
        ),
    }


def _port_property() -> dict:
    return {
        "type": "integer",
        "minimum": 1,
        "maximum": 65535,
        "description": (
            "Explicit destination port. Required: it must come from "
            "the selector, never defaulted from a registry IP tuple "
            "alone."
        ),
    }


def _target_run_input_schema(policy: ShellWorkroomPolicy | None) -> dict:
    """Language plus script plus registry selector plus explicit port."""
    timeout_max, script_max = _bounds(policy)
    properties = _script_properties(timeout_max, script_max)
    properties["target_ref"] = _target_ref_property(False)
    properties["port"] = _port_property()
    return {
        "type": "object",
        "properties": properties,
        "required": ["language", "script", "target_ref", "port"],
        "additionalProperties": False,
    }


def _install_run_input_schema(policy: ShellWorkroomPolicy | None) -> dict:
    """Language plus script plus mirror name only; port is operator-owned."""
    timeout_max, script_max = _bounds(policy)
    properties = _script_properties(timeout_max, script_max)
    properties["target_ref"] = _target_ref_property(True)
    return {
        "type": "object",
        "properties": properties,
        "required": ["language", "script", "target_ref"],
        "additionalProperties": False,
    }


def _descriptor_for(
    *,
    tool_name: str,
    operation: str,
    body_operation: str,
    input_schema: dict,
    output_schema: dict,
    description: str,
) -> ToolDescriptor:
    """Gated identity first, existing closed-provider body tag second."""
    return ToolDescriptor(
        tool_id=ToolId(SHELL_NAMESPACE + "." + tool_name),
        server_id=SHELL_SERVER_ID,
        tool_name=tool_name,
        description=description,
        input_schema=dict(input_schema),
        output_schema=dict(output_schema),
        operations=[operation, body_operation],
        target_types=list(TARGET_TYPES),
        capability_tags=[SHELL_NAMESPACE],
        risk_level=RiskLevel.MEDIUM,
        retry_safe=False,
        argument_bindings={},
        outbound_text_fields=[],
        target_binding=ToolTargetBinding(binds_target_arguments=True),
        requires_target=True,
        command_fields=["language", "script"],
        availability_impact=False,
    )


def build_gated_descriptors(policy: ShellWorkroomPolicy | None = None) -> list:
    """Return the three gated descriptors with policy-derived bounds."""
    target_schema = _target_run_input_schema(policy)
    install_schema = _install_run_input_schema(policy)
    return [
        _descriptor_for(
            tool_name=SHELL_TARGET_RUN,
            operation=TARGET_RUN_OPERATION,
            body_operation=RUN_OPERATION,
            input_schema=target_schema,
            output_schema=dict(RUN_OUTPUT_SCHEMA),
            description=_TARGET_RUN_DESCRIPTION,
        ),
        _descriptor_for(
            tool_name=SHELL_TARGET_JOB_START,
            operation=TARGET_JOB_START_OPERATION,
            body_operation=JOB_START_OPERATION,
            input_schema=target_schema,
            output_schema=dict(JOB_START_OUTPUT_SCHEMA),
            description=_TARGET_JOB_START_DESCRIPTION,
        ),
        _descriptor_for(
            tool_name=SHELL_INSTALL_RUN,
            operation=INSTALL_RUN_OPERATION,
            body_operation=RUN_OPERATION,
            input_schema=install_schema,
            output_schema=dict(RUN_OUTPUT_SCHEMA),
            description=_INSTALL_RUN_DESCRIPTION,
        ),
    ]


def gated_operation_for(tool_name: str) -> str | None:
    """Map a gated tool name to its operation, else None (no guessing)."""
    if tool_name in _GATED_TOOL_NAMES and tool_name in OPERATION_PURPOSE:
        return tool_name
    return None


def gated_purpose_for_operation(operation: str) -> NetworkPurpose | None:
    """Map an operation to its fixed network purpose, else None."""
    return OPERATION_PURPOSE.get(operation)


def gated_body_operation(descriptor: ToolDescriptor) -> str | None:
    """Pick the closed-provider body by existing operation tags only.

    Membership on the existing RUN/JOB_START constants decides the body;
    gated tool names never appear here. Anything unmappable yields None
    so the caller fails closed instead of executing the wrong body.
    """
    operations = set(descriptor.operations or [])
    if JOB_START_OPERATION in operations:
        return JOB_START_OPERATION
    if RUN_OPERATION in operations:
        return RUN_OPERATION
    return None


def _check_key_pattern(descriptor: ToolDescriptor) -> None:
    try:
        prop = descriptor.input_schema["properties"]["workroom_key"]
    except (KeyError, TypeError, AttributeError):
        raise ValueError("gated descriptor is missing the workroom_key property") from None
    if not isinstance(prop, dict) or prop.get("pattern") != WORKROOM_KEY_PATTERN:
        raise ValueError("gated descriptor carries an unexpected workroom_key pattern")
    required = descriptor.input_schema.get("required", [])
    if "workroom_key" in required:
        raise ValueError("gated descriptor must not require workroom_key")


def validate_gated_descriptors(
    descriptors: list, policy: ShellWorkroomPolicy | None = None
) -> None:
    """Check the three gated descriptors against the pinned provider shape."""
    if len(descriptors) != 3:
        raise ValueError("gated catalog must expose exactly three tools")
    seen = set()
    for descriptor in descriptors:
        validate_tool_manifest(descriptor)
        if descriptor.server_id != SHELL_SERVER_ID:
            raise ValueError("gated descriptor carries an unexpected server_id")
        if descriptor.tool_name in seen:
            raise ValueError("duplicate gated tool name")
        seen.add(descriptor.tool_name)
        if descriptor.tool_name not in _GATED_TOOL_NAMES:
            raise ValueError("gated descriptor is not part of the gated manifest")
        body = OPERATION_BODY.get(descriptor.tool_name)
        if body is None or descriptor.operations != [descriptor.tool_name, body]:
            raise ValueError("gated descriptor operations must be [gated-op, body-op]")
        if (
            descriptor.risk_level != RiskLevel.MEDIUM
            or descriptor.command_fields != ["language", "script"]
            or descriptor.requires_target is not True
            or list(descriptor.target_types) != TARGET_TYPES
        ):
            raise ValueError("gated descriptor differs from the pinned manifest")
        _check_key_pattern(descriptor)
        properties = descriptor.input_schema.get("properties", {})
        if "target_ref" not in properties:
            raise ValueError("gated descriptor is missing the target_ref selector")
        required = descriptor.input_schema.get("required", [])
        if descriptor.tool_name == SHELL_INSTALL_RUN:
            if "port" in properties:
                raise ValueError("install descriptor must not take a port selector")
            if required != ["language", "script", "target_ref"]:
                raise ValueError("install descriptor has unexpected required fields")
        else:
            if "port" not in properties:
                raise ValueError("target descriptor is missing the port selector")
            if required != ["language", "script", "target_ref", "port"]:
                raise ValueError("target descriptor has unexpected required fields")
        if gated_body_operation(descriptor) != body:
            raise ValueError("gated descriptor body tag does not resolve")
    if seen != set(_GATED_TOOL_NAMES):
        raise ValueError("gated catalog is missing pinned tools")
    if policy is not None:
        reference = {d.tool_name: d for d in build_gated_descriptors(policy)}
        for descriptor in descriptors:
            expected = reference[descriptor.tool_name].input_schema.get("properties", {})
            actual = descriptor.input_schema.get("properties", {})
            pairs = (("timeout_seconds", "maximum"), ("script", "maxLength"))
            for prop_name, subfield in pairs:
                if prop_name in expected:
                    want = expected[prop_name][subfield]
                    got = actual.get(prop_name, {}).get(subfield)
                    if got != want:
                        raise ValueError("gated bounds differ from the active policy")
