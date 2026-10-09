"""One-call shell provider assembly for CLI composition callers.

Owned by the Capability team. TASK-008: CLI run/resume/tools share this
single assembly so the adapter and the program-discovery source are always
the same workroom instance. No core registry change, no server/tool-name
branching: the returned adapter plugs into the existing generic
builtin_adapters seam (capability/bootstrap._build_gateway) and the
program_source fills the existing RuntimeDeps.shell_programs port.

A None policy path means disabled: this returns None and callers skip the
shell wiring, leaving an honest catalog without shell tools.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from redteam.capability.settings import MCPConfigurationError
from redteam.capability.shell_adapter import BuiltinShellAdapter
from redteam.capability.shell_assembly import build_shell_entries, shell_server_output_limits
from redteam.capability.shell_manifest import SHELL_SERVER_ID
from redteam.capability.shell_policy import ShellWorkroomPolicy, load_shell_workroom_policy
from redteam.core.contracts.tool import ToolDescriptor
from redteam.core.ports.program_discovery import ShellProgramDiscovery


@dataclass(frozen=True)
class ShellRuntime:
    """Validated shell provider pieces, ready for the generic seams.

    adapter serves the four shell tools through the gateway; program_source
    is the SAME workroom instance the adapter executes against, so live
    program rows and script execution never split state.
    """

    policy: ShellWorkroomPolicy
    adapter: BuiltinShellAdapter
    program_source: ShellProgramDiscovery
    descriptors: list[ToolDescriptor] = field(default_factory=list)
    concurrency: dict[str, int] = field(default_factory=dict)
    output_limits: dict[str, int] = field(default_factory=dict)


def build_shell_runtime(policy_path: str | None) -> ShellRuntime | None:
    """Build the shell provider from an operator policy path, or None.

    None (undeclared REDTEAM_SHELL_POLICY_PATH) means disabled and yields
    None. Any other failure -- unreadable file, bad JSON, schema
    violation, unpinned image -- raises MCPConfigurationError so CLI
    callers report a config error instead of silently running without
    the operator-requested shell provider.
    """
    if policy_path is None:
        return None
    try:
        policy = load_shell_workroom_policy(policy_path)
    except (OSError, ValueError) as exc:
        raise MCPConfigurationError(
            f"shell workroom policy invalid ({policy_path}): {exc}"
        ) from exc
    descriptors, adapter = build_shell_entries(policy)
    program_source = adapter.program_source
    return ShellRuntime(
        policy=policy,
        adapter=adapter,
        program_source=program_source,
        descriptors=list(descriptors),
        concurrency={SHELL_SERVER_ID: policy.max_jobs},
        output_limits=dict(shell_server_output_limits(policy)),
    )
