"""Public activation and injection seam for gated windows."""

# Wiring (Control side): select policy (absent means closed).
# Run existing check_scope for the approved registry name, pin literal
# IPs with deny winning across aliases, pass triples plus holder identity
# from the trusted ToolInvocation, consume the SYSTEM companion at
# settle. Provider sees IP, port, quantity only.
from __future__ import annotations

from dataclasses import dataclass
from functools import partial
from typing import cast

from redteam.capability.gate_observer import RunDocker
from redteam.capability.gated_manifest import (
    build_gated_descriptors,
    validate_gated_descriptors,
)
from redteam.capability.gated_network_policy import GatedNetworkPolicy
from redteam.capability.gated_strategy import GatedExecutionPolicy, ScopeValidator
from redteam.capability.gated_window import GATED_QUEUE_NUM, AllowedFlow, GatedWindowExtension
from redteam.capability.shell_runtime import ShellRuntime
from redteam.capability.shell_workroom import ShellWorkroomManager
from redteam.core.contracts.engagement import EngagementScope
from redteam.core.contracts.network import NetworkGrant, NetworkPurpose
from redteam.core.contracts.tool import ToolDescriptor
from redteam.core.ports.evidence_store import EvidenceStore
from redteam.core.ports.network_grants import NetworkGrantLedger
from redteam.core.scope_rules import check_target_scope

DockerRunner = RunDocker
"""Public read-only docker runner: (argv, stdin, timeout, limit) to a result."""


@dataclass(frozen=True)
class GatedActivation:
    """Control-held activation bundle: authority plus operator selection."""

    ledger: NetworkGrantLedger
    policy: GatedNetworkPolicy
    queue_num: int = GATED_QUEUE_NUM


def build_gated_extension(
    manager: ShellWorkroomManager,
    activation: GatedActivation,
    docker_runner: DockerRunner,
) -> GatedWindowExtension:
    """Inject the authority and policy into one manager-bound extension."""
    ext = GatedWindowExtension(
        manager=manager,
        ledger=activation.ledger,
        run_docker=docker_runner,
        policy=activation.policy,
        queue_num=activation.queue_num,
    )
    manager.set_execution_admission(ext)
    return ext


async def open_gated_window(
    extension: GatedWindowExtension,
    *,
    run_id: str,
    instance_id: str,
    epoch: int,
    purpose: NetworkPurpose,
    allowed: tuple[AllowedFlow, ...],
    holder_action_id: str,
    holder_invocation_id: str,
    holder_work_id: str | None = None,
    internal: bool = False,
) -> NetworkGrant:
    """Resolve operator caps for the purpose and open one window."""
    policy = extension.policy
    if policy is None:
        from redteam.capability.gated_window import GatedNetworkError

        raise GatedNetworkError("gated_network_unconfigured")
    caps = policy.caps_for(purpose)
    return await extension.open_window(
        run_id=run_id,
        instance_id=instance_id,
        epoch=epoch,
        caps=caps,
        allowed=allowed,
        holder_action_id=holder_action_id,
        holder_invocation_id=holder_invocation_id,
        holder_work_id=holder_work_id,
        internal=internal,
    )


@dataclass(frozen=True)
class GatedProviderConfig:
    """Operator-owned gated provider configuration (never model input)."""

    network_policy: GatedNetworkPolicy
    queue_num: int = GATED_QUEUE_NUM
    docker_runner: DockerRunner | None = None


@dataclass(frozen=True)
class GatedShellAssembly:
    """Configured gated pieces, ready for gateway discovery attachment."""

    extension: GatedWindowExtension
    executor: GatedExecutionPolicy
    descriptors: list[ToolDescriptor]


def configure_gated(
    shell: ShellRuntime,
    scope: EngagementScope,
    ledger: NetworkGrantLedger,
    evidence: EvidenceStore,
    *,
    provider_config: GatedProviderConfig,
) -> GatedShellAssembly:
    """Compose the gated capability after stores exist, before discovery.

    Call order in bootstrap: build the engagement stores (including the
    Control-owned network ledger), then call this, then run gateway
    discovery over shell descriptors plus the returned gated descriptors.
    Attaching the executor to adapter dispatch is a separate small hook
    owned by the adapter worker; this function only builds and validates.
    """
    manager = shell.adapter.program_source
    runner = provider_config.docker_runner
    if runner is None:
        runner = manager._run_docker
    activation = GatedActivation(
        ledger=ledger,
        policy=provider_config.network_policy,
        queue_num=provider_config.queue_num,
    )
    extension = build_gated_extension(manager, activation, runner)
    validator = cast(ScopeValidator, partial(check_target_scope, scope=scope))
    executor = GatedExecutionPolicy(
        extension, scope_validator=validator, evidence_store=evidence
    )
    descriptors = build_gated_descriptors(shell.adapter.policy)
    validate_gated_descriptors(descriptors, shell.adapter.policy)
    return GatedShellAssembly(
        extension=extension, executor=executor, descriptors=descriptors
    )
