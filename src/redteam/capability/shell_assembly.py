"""Assembly helper for the builtin shell adapter. Owned by Capability Team.

TASK-003 deliberately does NOT touch ``capability/bootstrap.py``,
``capability/gateway.py``, ``capability/catalog.py``, or
``app/bootstrap.py`` (owned by the parallel catalog work and the
composition root). This module builds the ready-to-plug pieces and
documents the exact seam the assembler must use. Nothing here opens an
MCP transport or reaches orchestration / control / contracts code.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import ValidationError

from redteam.capability.gated_network_policy import GatedNetworkPolicy
from redteam.capability.settings import MCPConfigurationError
from redteam.capability.shell_adapter import BuiltinShellAdapter
from redteam.capability.shell_manifest import (
    SHELL_SERVER_ID,
    build_shell_descriptors,
    validate_shell_descriptors,
)
from redteam.capability.shell_policy import ShellWorkroomPolicy, validation_diagnostic
from redteam.capability.shell_workroom import (
    ProgramRecord as ProgramRecord,
)
from redteam.capability.shell_workroom import (
    ShellWorkroomManager,
)
from redteam.capability.shell_workroom import (
    default_program_manifest_path as default_program_manifest_path,
)
from redteam.core.contracts.tool import ToolDescriptor

if TYPE_CHECKING:
    from redteam.capability.gated_activation import GatedProviderConfig

# Plug-in seam for the catalog/bootstrap owner (no code change needed here):
#
#     from redteam.capability.shell_assembly import build_shell_entries
#     descriptors, adapter = build_shell_entries(policy)
#     registry.replace_server_tools(SHELL_SERVER_ID, descriptors)
#     adapters[SHELL_SERVER_ID] = adapter
#     for descriptor in descriptors:
#         operation_bindings[str(descriptor.tool_id)] = tuple(descriptor.operations)
#
# Preferred (bootstrap seam):
#
#     from redteam.capability import bootstrap as capability_bootstrap
#     from redteam.capability.shell_assembly import (
#         build_shell_entries, shell_server_output_limits,
#     )
#     descriptors, adapter = build_shell_entries(policy)
#     gateway = await capability_bootstrap.build_mcp_tool_gateway(
#         [],
#         builtin_adapters=[adapter],
#         builtin_concurrency={adapter.server_id: policy.max_jobs},
#         builtin_output_limits=shell_server_output_limits(policy),
#     )
#
# The gateway then serves the shell tools through its existing invoke and
# admission paths, and the Control plane reads their risk levels from the
# same registry snapshot as every other tool. When no shell policy is
# configured the assembler simply skips this helper, which yields an
# honest catalog without shell tools.
ASSEMBLY_SEAM_DOC = "shell_assembly.build_shell_entries"




# Worst-case serialized size of one policy-bounded result: two streams of
# max_output_bytes UTF-8 bytes each, where every byte can expand to a
# 6-character JSON escape (ensure_ascii=False still escapes ASCII control
# characters as \uXXXX), plus envelope slack for the fixed result fields.
# The gateway applies this outer cut after the adapter inner per-stream
# collection cap; the outer bound must never clip a legitimate bounded
# result first.
_JSON_ESCAPE_EXPANSION = 6
_STREAM_COUNT = 2
_OUTPUT_ENVELOPE_SLACK_BYTES = 65536


def shell_output_limit(policy: ShellWorkroomPolicy) -> int:
    """Outer per-server serialized-output bound derived from the policy."""
    return (
        _STREAM_COUNT * _JSON_ESCAPE_EXPANSION * policy.max_output_bytes
        + _OUTPUT_ENVELOPE_SLACK_BYTES
    )


def shell_server_output_limits(policy: ShellWorkroomPolicy) -> dict[str, int]:
    """Gateway ``builtin_output_limits`` mapping for the shell provider."""
    return {SHELL_SERVER_ID: shell_output_limit(policy)}




def shell_operation_bindings(
    descriptors: list[ToolDescriptor],
) -> dict[str, tuple[str, ...]]:
    # Map tool_id to operations for the gateway constructor seam.
    return {str(descriptor.tool_id): tuple(descriptor.operations) for descriptor in descriptors}


def build_shell_adapter(
    policy: ShellWorkroomPolicy,
    *,
    execution_admission=None,
    program_manifest_path: str | None = None,
) -> BuiltinShellAdapter:
    # TASK-005 admission hook; None keeps the unrestricted closed path.
    policy.validate_image()
    workroom = ShellWorkroomManager(policy, program_manifest_path=program_manifest_path)
    if execution_admission is not None:
        workroom.set_execution_admission(execution_admission)
    return BuiltinShellAdapter(policy, workroom=workroom)


def build_shell_entries(
    policy: ShellWorkroomPolicy,
    *,
    execution_admission=None,
    program_manifest_path: str | None = None,
) -> tuple[list[ToolDescriptor], BuiltinShellAdapter]:
    # Gateway seam; passes the admission hook through.
    adapter = build_shell_adapter(
        policy, execution_admission=execution_admission,
        program_manifest_path=program_manifest_path,
    )
    descriptors = build_shell_descriptors(policy)
    validate_shell_descriptors(descriptors, policy)
    return descriptors, adapter


def build_gated_provider_config(
    policy_path: str | None, *, policy_bytes: bytes | None = None,
) -> GatedProviderConfig | None:
    """Build the gated-network provider config, or None when closed.

    None (undeclared REDTEAM_GATED_NETWORK_POLICY_PATH) means gated egress
    stays closed and yields None: callers pass no ``gated_shell`` tuple into
    the composition root and the shell keeps its network-closed path. Any
    other failure -- unreadable file, bad JSON, schema violation -- raises
    MCPConfigurationError so CLI callers report a config error instead of
    silently running closed despite the operator requesting gated egress.
    Schema violations carry only a bounded field/type diagnostic (never raw
    input values), shared with the shell policy loader.
    """
    if policy_path is None:
        return None
    from redteam.capability.gated_activation import GatedProviderConfig

    try:
        raw = (Path(policy_path).read_text(encoding="utf-8") if policy_bytes is None
               else policy_bytes.decode("utf-8"))
    except OSError as exc:
        raise MCPConfigurationError(
            f"gated network policy unreadable ({policy_path}): {exc}"
        ) from exc
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise MCPConfigurationError(
            f"gated network policy is not valid JSON ({policy_path}): {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise MCPConfigurationError(
            f"gated network policy must be a JSON object ({policy_path})"
        )
    try:
        policy = GatedNetworkPolicy(**data)
    except ValidationError as exc:
        raise MCPConfigurationError(
            f"gated network policy invalid ({policy_path}): {validation_diagnostic(exc)}"
        ) from exc
    try:
        policy.require_sidecar_image()
    except ValueError as exc:
        raise MCPConfigurationError(str(exc)) from None
    return GatedProviderConfig(network_policy=policy)
