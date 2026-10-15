"""One operator selection for web and CLI target-network startup."""

from collections.abc import Callable
from urllib.parse import urlsplit

from redteam.app.web_policy import (
    KALI_NETWORK_MODE_KEY,
    WEB_POLICY_KEY,
    WebPolicySelection,
    selected_target_networks,
    validate_kali_network_policy,
)
from redteam.capability.deployment import check_origin_scope
from redteam.capability.direct_target import (
    DirectTargetConfig,
    DirectTargetPreparationError,
    PreparedTargetNetwork,
    prepare_target_network,
)
from redteam.capability.servers.network import resolve_target
from redteam.capability.shell_runtime import ShellRuntime
from redteam.core.config import EngagementConfig


def kali_selection(engagement: EngagementConfig) -> WebPolicySelection | None:
    raw = engagement.metadata.get(WEB_POLICY_KEY)
    return WebPolicySelection.model_validate(raw) if raw is not None else None


def selected_target_ports(selection: WebPolicySelection) -> dict[str, tuple[int, ...]]:
    ports: dict[str, tuple[int, ...]] = {}
    for origin in selection.origins:
        parsed = urlsplit(origin)
        assert parsed.hostname is not None
        ports[parsed.hostname] = (parsed.port or (443 if parsed.scheme == "https" else 80),)
    return ports


async def prepare_kali_network(
    engagement: EngagementConfig,
    shell: ShellRuntime | None,
    *,
    selection: WebPolicySelection | None = None,
    auto_setup: bool = True,
    progress: Callable[[str], None] | None = None,
    expected_fingerprint: str | None = None,
) -> PreparedTargetNetwork | None:
    """Prepare direct Kali connectivity selected by the engagement.

    Legacy ``configured`` selections are read once and projected to the
    direct target/port/bridge fields.  Sidecar, quota and flow settings are
    intentionally not carried into the active runtime.
    """
    selection = selection if selection is not None else kali_selection(engagement)
    network: DirectTargetConfig | None = None
    if selection is None and engagement.metadata.get(KALI_NETWORK_MODE_KEY) == "closed":
        network = None
    elif selection is None or selection.kali_network_mode == "inherit":
        network = None
    elif selection.kali_network_mode != "closed":
        for origin in selection.origins:
            check_origin_scope(origin, engagement.scope)
        spec = selection.spec(await selected_target_networks(selection, engagement))
        targets = {}
        for origin in selection.origins:
            host = urlsplit(origin).hostname
            assert host is not None
            resolved = await resolve_target(spec.policy, spec.host_bindings[host])
            targets[host] = tuple(sorted(resolved.addresses))
        if selection.kali_network_mode == "prepare":
            assert selection.kali_network_preparation is not None
            network = DirectTargetConfig(
                targets=targets,
                target_ports=selected_target_ports(selection),
                target_network_id=selection.kali_network_preparation.target_network_id,
            )
        else:
            assert selection.kali_network_policy_path is not None
            _, provider = validate_kali_network_policy(
                selection.kali_network_policy_path,
                selection.origins,
                expected_sha256=selection.kali_network_policy_sha256,
            )
            legacy = provider.network_policy
            selected_ports = selected_target_ports(selection)
            if any(
                set(ports) - set(selected_ports[name])
                for name, ports in legacy.target_ports.items()
            ):
                raise DirectTargetPreparationError(
                    "direct_target_port_mismatch",
                    "kali_network_policy_path",
                    "Restore ports matching the explicitly selected origins.",
                )
            target_ports = {
                name: legacy.target_ports.get(name, selected_ports[name])
                for name in legacy.registry.targets
            }
            network = DirectTargetConfig(
                targets=legacy.registry.targets,
                target_ports=target_ports,
                target_network_id=legacy.target_network_id,
            )
            if any(set(ips) - set(targets[host]) for host, ips in network.targets.items()):
                raise DirectTargetPreparationError(
                    "direct_target_pin_mismatch",
                    "kali_network_policy_path",
                    "Restore pins matching the explicitly selected hosts.",
                )
    if network is None:
        if expected_fingerprint is not None:
            raise DirectTargetPreparationError(
                "direct_target_preparation_changed",
                "kali_network_mode",
                "Restore the Run's original target-network selection.",
            )
        return None
    if shell is None:
        raise DirectTargetPreparationError(
            "direct_target_shell_unavailable",
            "kali_network_mode",
            "Prepare Kali or select an existing shell policy first.",
        )
    return await prepare_target_network(
        shell.policy,
        network,
        shell_fingerprint=shell.environment_fingerprint,
        progress=progress,
        expected_fingerprint=expected_fingerprint,
    )
