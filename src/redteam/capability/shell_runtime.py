"""One-call shell provider assembly for CLI composition callers.

Owned by the Capability team. TASK-008: CLI run/resume/tools share this
single assembly so the adapter and the program-discovery source are always
the same workroom instance. No core registry change, no server/tool-name
branching: the returned adapter plugs into the existing generic
builtin_adapters seam (capability/bootstrap._build_gateway) and the
program_source fills the existing RuntimeDeps.shell_programs port.

The pure builder treats a None policy path as disabled. Execution callers
use the async preparation entry point to prepare a managed default; inspection
callers never launch Docker or build images.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace

from redteam.capability.settings import MCPConfigurationError
from redteam.capability.shell_adapter import BuiltinShellAdapter
from redteam.capability.shell_assembly import build_shell_entries, shell_server_output_limits
from redteam.capability.shell_manifest import SHELL_SERVER_ID
from redteam.capability.shell_policy import ShellWorkroomPolicy, load_shell_workroom_policy
from redteam.capability.shell_profiles import DEFAULT_SHELL_IMAGE_PROFILE
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
    environment_fingerprint: str | None = None


def build_shell_runtime(
    policy_path: str | None, *, policy: ShellWorkroomPolicy | None = None,
    program_manifest_path: str | None = None,
) -> ShellRuntime | None:
    """Build the shell provider from an operator policy path, or None.

    None (undeclared REDTEAM_SHELL_POLICY_PATH) means disabled and yields
    None. Any other failure -- unreadable file, bad JSON, schema
    violation, unpinned image -- raises MCPConfigurationError so CLI
    callers report a config error instead of silently running without
    the operator-requested shell provider.
    """
    if policy_path is None and policy is None:
        return None
    try:
        if policy is None:
            assert policy_path is not None
            policy = load_shell_workroom_policy(policy_path)
        policy.validate_image()
    except (OSError, ValueError) as exc:
        raise MCPConfigurationError(
            f"shell workroom policy invalid ({policy_path}): {exc}"
        ) from exc
    descriptors, adapter = build_shell_entries(policy, program_manifest_path=program_manifest_path)
    program_source = adapter.program_source
    return ShellRuntime(
        policy=policy,
        adapter=adapter,
        program_source=program_source,
        descriptors=list(descriptors),
        concurrency={SHELL_SERVER_ID: policy.max_jobs},
        output_limits=dict(shell_server_output_limits(policy)),
    )


async def prepare_shell_runtime(
    policy_path: str | None, *, auto_setup: bool = True,
    progress: Callable[[str], None] | None = None,
    expected_fingerprint: str | None = None,
    docker_context: str | None = None,
    image_profile: str = DEFAULT_SHELL_IMAGE_PROFILE,
) -> ShellRuntime | None:
    """Prepare Docker only at execution startup, then use the pure assembly.

    Manual mode preserves the prior policy-only behavior. Inspection callers
    continue using build_shell_runtime and never launch or build anything.
    """
    if not auto_setup:
        if expected_fingerprint is not None:
            raise MCPConfigurationError(
                "This run requires its prepared execution environment; "
                "enable REDTEAM_SHELL_AUTO_SETUP to resume it."
            )
        runtime = build_shell_runtime(policy_path)
        if docker_context is not None and (
            runtime is None or runtime.policy.docker_context != docker_context
        ):
            raise MCPConfigurationError("Manual shell policy must pin the selected Docker context.")
        return runtime
    from redteam.capability.shell_preparation import prepare_shell_policy

    options = {"docker_context": docker_context} if docker_context is not None else {}
    if image_profile != DEFAULT_SHELL_IMAGE_PROFILE:
        options["image_profile"] = image_profile
    prepared = await prepare_shell_policy(
        policy_path, progress=progress, expected_fingerprint=expected_fingerprint, **options,
    )
    runtime = build_shell_runtime(
        None, policy=prepared.policy, program_manifest_path=prepared.manifest_path,
    )
    assert runtime is not None
    return replace(runtime, environment_fingerprint=prepared.fingerprint)
