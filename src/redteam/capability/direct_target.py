"""Direct target networking for the builtin Kali workroom.

This provider deliberately has no packet gate, flow ledger, NFQUEUE daemon,
or byte quota.  It validates the selected logical target through the normal
Control scope boundary, attaches the workroom to the selected Docker bridge
for one synchronous execution, and detaches it afterwards.  The script runs
with ordinary container networking and can therefore make connections other
than the selector it declared; callers and UI must not describe this as an
egress allowlist.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from redteam.capability.adapters.mcp.base import MCPCallResult
from redteam.capability.manifest import validate_tool_manifest
from redteam.capability.settings import MCPConfigurationError
from redteam.capability.shell_image import run_preparation_command
from redteam.capability.shell_manifest import (
    RUN_OPERATION,
    RUN_OUTPUT_SCHEMA,
    SHELL_LANGUAGES,
    SHELL_NAMESPACE,
    SHELL_SERVER_ID,
)
from redteam.capability.shell_policy import WORKROOM_KEY_PATTERN, ShellWorkroomPolicy
from redteam.capability.shell_preparation import (
    preparation_cache_directory,
    preparation_lock,
    resolve_docker_endpoint,
    write_preparation_record,
)
from redteam.capability.shell_runtime import ShellRuntime
from redteam.capability.shell_workroom import (
    ExecutionAdmission,
    ShellWorkroomError,
    ShellWorkroomManager,
    workroom_key_for,
)
from redteam.core.contracts.target import AnyTarget, EndpointTarget
from redteam.core.contracts.tool import ToolDescriptor, ToolError, ToolInvocation, ToolTargetBinding
from redteam.core.enums import RiskLevel
from redteam.core.ids import ToolId
from redteam.core.ports.program_discovery import ProgramExecutionRoute
from redteam.core.scope_rules import resolve_target_hostports

SHELL_TARGET_RUN = "shell_target_run"
TARGET_RUN_OPERATION = SHELL_TARGET_RUN
TARGET_TYPES = ["host", "domain", "url", "endpoint", "service"]
DIRECT_NETWORK_LABEL = "io.whs-redteam.direct-target"


class DirectTargetError(ValueError):
    """Stable direct-network refusal code."""


class DirectTargetPreparationError(MCPConfigurationError):
    def __init__(self, code: str, field: str, repair: str):
        self.code, self.field, self.repair_condition = code, field, repair
        super().__init__(f"{code} ({field}): {repair}")


class TargetNetworkPreparation(BaseModel):
    """Docker selection for direct Kali-to-target connectivity.

    ``extra=ignore`` is intentional for one-way migration of saved plan7
    drafts: obsolete sidecar/quota fields are accepted when read and omitted
    when the engagement is next saved.
    """

    model_config = ConfigDict(extra="ignore", frozen=True, hide_input_in_errors=True)
    docker_context: str | None = Field(default=None, title="Docker context (빈 값: 현재 설정)")
    target_network_id: str | None = Field(
        default=None,
        pattern=r"^[a-f0-9]{64}$",
        title="대상 Docker bridge ID (빈 값: 일반 Docker 네트워크)",
    )

    @field_validator("docker_context")
    @classmethod
    def valid_context(cls, value: str | None) -> str | None:
        if value is not None and (
            not value or len(value) > 256 or any(char.isspace() or char == "\x00" for char in value)
        ):
            raise ValueError("docker_context must be one Docker context name")
        return value


class DirectTargetConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    targets: dict[str, tuple[str, ...]]
    target_ports: dict[str, tuple[int, ...]]
    target_network_id: str | None = None

    @field_validator("targets")
    @classmethod
    def valid_targets(cls, value: dict[str, tuple[str, ...]]):
        if not value:
            raise ValueError("direct target registry is empty")
        cleaned: dict[str, tuple[str, ...]] = {}
        for name, addresses in value.items():
            if not name or not addresses:
                raise ValueError("direct target registry entry is empty")
            normalized = tuple(str(ipaddress.IPv4Address(item)) for item in addresses)
            if normalized != addresses:
                raise ValueError("direct target addresses must be canonical IPv4 literals")
            cleaned[name] = normalized
        return cleaned

    @field_validator("target_ports")
    @classmethod
    def valid_ports(cls, value: dict[str, tuple[int, ...]]):
        for name, ports in value.items():
            if not name or not ports or any(isinstance(port, bool) or not 0 < port <= 65535
                                            for port in ports):
                raise ValueError("direct target ports must be non-empty TCP port lists")
            if len(set(ports)) != len(ports):
                raise ValueError("direct target ports must be unique")
        return value

    @model_validator(mode="after")
    def matching_selectors(self):
        if set(self.targets) != set(self.target_ports):
            raise ValueError("direct target and port selectors must match")
        return self


@dataclass(frozen=True)
class DirectTargetProviderConfig:
    network: DirectTargetConfig
    target_route_identity: dict
    docker_endpoint: dict


@dataclass(frozen=True)
class PreparedTargetNetwork:
    provider: DirectTargetProviderConfig
    fingerprint: str
    docker_context: str


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


async def _inspect(policy: ShellWorkroomPolicy, kind: str, identity: str) -> dict:
    template = (
        '{"Id":{{json .Id}},"Name":{{json .Name}},"Driver":{{json .Driver}},'
        '"Containers":{{json .Containers}}}'
        if kind == "network"
        else '{"Id":{{json .Id}},"Image":{{json .Image}},'
        '"State":{"Running":{{json .State.Running}}},'
        '"NetworkSettings":{"Networks":{{json .NetworkSettings.Networks}}}}'
    )
    code, output, _ = await run_preparation_command(
        policy.docker_command(kind, "inspect", identity, "--format", template), 30
    )
    if code:
        raise DirectTargetPreparationError(
            "direct_target_route_unavailable", "target_network_id",
            "Select a running target and bridge on the selected Docker context.",
        )
    try:
        value = json.loads(output)
    except ValueError:
        raise DirectTargetPreparationError(
            "direct_target_route_unverifiable", "target_network_id",
            "Restore Docker inspect access for the selected target route.",
        ) from None
    if not isinstance(value, dict):
        raise DirectTargetPreparationError(
            "direct_target_route_unverifiable", "target_network_id",
            "Restore Docker inspect access for the selected target route.",
        )
    return value


async def _route_identity(policy: ShellWorkroomPolicy, network: DirectTargetConfig) -> dict:
    identity: dict[str, Any] = {
        "connection": "docker_routed",
        "target_ips": sorted({ip for values in network.targets.values() for ip in values}),
    }
    if network.target_network_id is None:
        return identity
    record = await _inspect(policy, "network", network.target_network_id)
    if record.get("Id") != network.target_network_id or record.get("Driver") != "bridge":
        raise DirectTargetPreparationError(
            "direct_target_network_mismatch", "target_network_id",
            "Select the full ID of a Docker bridge on this context.",
        )
    members = record.get("Containers")
    if not isinstance(members, dict):
        raise DirectTargetPreparationError(
            "direct_target_route_unverifiable", "target_network_id",
            "Restore Docker inspect access for the selected bridge.",
        )
    containers: dict[str, dict[str, str]] = {}
    for address in identity["target_ips"]:
        matches = [
            container_id for container_id, member in members.items()
            if isinstance(member, dict) and member.get("IPv4Address", "").split("/")[0] == address
        ]
        if len(matches) != 1:
            raise DirectTargetPreparationError(
                "direct_target_container_pin_mismatch", "target_network_id",
                "Use the current target container IPv4 and its attached bridge.",
            )
        container = await _inspect(policy, "container", matches[0])
        attachment = (
            container.get("NetworkSettings", {}).get("Networks", {}).get(record.get("Name"), {})
        )
        if (
            container.get("Id") != matches[0]
            or container.get("State", {}).get("Running") is not True
            or attachment.get("NetworkID") != network.target_network_id
            or attachment.get("IPAddress") != address
        ):
            raise DirectTargetPreparationError(
                "direct_target_container_identity_mismatch", "target_network_id",
                "Restart preparation with the current target container identity.",
            )
        containers[address] = {
            "container_id": matches[0], "image_id": str(container.get("Image", ""))
        }
    return {
        **identity,
        "connection": "operator_bridge",
        "network_id": record["Id"],
        "network_name": record["Name"],
        "containers": containers,
    }


async def prepare_target_network(
    shell_policy: ShellWorkroomPolicy,
    network: DirectTargetConfig,
    *,
    shell_fingerprint: str | None,
    progress: Callable[[str], None] | None = None,
    expected_fingerprint: str | None = None,
) -> PreparedTargetNetwork:
    """Pin Docker and route identity without a sidecar or target request."""
    try:
        endpoint = await resolve_docker_endpoint(shell_policy)
        policy = ShellWorkroomPolicy.model_validate({
            **shell_policy.model_dump(),
            "docker_binary": endpoint.binary,
            "docker_context": endpoint.context,
        })
        code, engine, _ = await run_preparation_command(
            policy.docker_command("info", "--format", "{{.OSType}}"), 30
        )
        if code or engine.strip() != "linux":
            raise DirectTargetPreparationError(
                "direct_target_docker_unavailable", "docker_context",
                "Start the selected Linux Docker engine.",
            )
        if progress:
            progress(f"Checking direct Kali target route ({endpoint.context})...")
        route = await _route_identity(policy, network)
        identity = {
            "version": 1,
            "network": network.model_dump(mode="json"),
            "shell_fingerprint": shell_fingerprint,
            "route": route,
            "docker_endpoint": asdict(endpoint),
        }
        fingerprint = _hash(identity)
        if expected_fingerprint is not None and fingerprint != expected_fingerprint:
            raise DirectTargetPreparationError(
                "direct_target_preparation_changed", "kali_network_mode",
                "Restore the original Docker context, target addresses and bridge.",
            )
        cache = preparation_cache_directory()
        records = cache / "records"
        records.mkdir(mode=0o700, parents=True, exist_ok=True)
        async with preparation_lock(cache / "prep.lock"):
            destination = records / ("direct-target-" + fingerprint + ".json")
            if destination.exists():
                if json.loads(destination.read_text()) != identity:
                    raise DirectTargetPreparationError(
                        "direct_target_preparation_changed", "kali_network_mode",
                        "Restore the original direct target preparation.",
                    )
            else:
                write_preparation_record(destination, identity)
        if progress:
            progress("Direct Kali target route prepared; target reachability is untested.")
        return PreparedTargetNetwork(
            provider=DirectTargetProviderConfig(
                network=network,
                target_route_identity=route,
                docker_endpoint=asdict(endpoint),
            ),
            fingerprint=fingerprint,
            docker_context=endpoint.context,
        )
    except DirectTargetPreparationError:
        raise
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise DirectTargetPreparationError(
            "direct_target_preparation_unavailable", "kali_network_mode",
            f"Restore the Docker target route ({type(exc).__name__}).",
        ) from None


@dataclass
class _Attached:
    network_name: str
    holder_invocation_id: str
    created: bool


class DirectTargetNetworkExtension(ExecutionAdmission):
    def __init__(
        self,
        manager: ShellWorkroomManager,
        provider: DirectTargetProviderConfig,
    ) -> None:
        self.manager = manager
        self.provider = provider
        self.policy = provider.network
        self._open: dict[str, _Attached] = {}
        self._opening: set[str] = set()

    def check_exec_allowed(self, key: str, *, invocation_id: str | None = None) -> None:
        attached = self._open.get(key)
        if attached is not None and invocation_id != attached.holder_invocation_id:
            raise ShellWorkroomError("direct_target_busy")

    def inventory_mode(self, key: str) -> str:
        return "static" if key in self._open or key in self._opening else "full"

    def expected_networks(self, key: str) -> frozenset[str]:
        attached = self._open.get(key)
        return frozenset({attached.network_name}) if attached else frozenset({"none"})

    def expected_network_ids(self, key: str) -> dict[str, str]:
        attached = self._open.get(key)
        route = self.provider.target_route_identity
        if attached and route.get("network_id"):
            return {attached.network_name: str(route["network_id"])}
        return {}

    def program_execution_routes(self):
        return [ProgramExecutionRoute(
            purpose="target",
            status="target_ready",
            reason_code="direct_docker_route_prepared",
            tool_ids=[SHELL_NAMESPACE + "." + SHELL_TARGET_RUN],
            selectors={
                name: {"ips": list(ips), "ports": list(self.policy.target_ports.get(name, ()))}
                for name, ips in self.policy.targets.items()
            },
            limits={"network_enforcement": "none"},
        )]

    async def open(self, key: str, *, holder_invocation_id: str) -> None:
        if key in self._open or key in self._opening:
            raise DirectTargetError("direct_target_busy")
        self._opening.add(key)
        room = None
        network_name = ""
        created = False
        try:
            endpoint = await resolve_docker_endpoint(self.manager.policy)
            if asdict(endpoint) != self.provider.docker_endpoint:
                raise DirectTargetError("direct_target_docker_endpoint_changed")
            room = await self.manager.ensure_workroom(key)
            route = await _route_identity(self.manager.policy, self.policy)
            if route != self.provider.target_route_identity:
                raise DirectTargetError("direct_target_route_changed")
            network_name = str(route.get("network_name") or ("dtn-" + uuid.uuid4().hex[:16]))
            if "network_name" not in route:
                code, _, _, timed_out, _ = await self.manager._run_docker(
                    ("network", "create", "--driver", "bridge", "--label", DIRECT_NETWORK_LABEL,
                     network_name), None, 30.0, None,
                )
                if code or timed_out:
                    raise DirectTargetError("direct_target_network_create_failed")
                created = True
            code, _, _, timed_out, _ = await self.manager._run_docker(
                ("network", "disconnect", "none", room), None, 30.0, None,
            )
            if code and not await self._networkless(room):
                raise DirectTargetError("direct_target_detach_closed_failed")
            code, _, _, timed_out, _ = await self.manager._run_docker(
                ("network", "connect", network_name, room), None, 30.0, None,
            )
            if code or timed_out:
                raise DirectTargetError("direct_target_connect_failed")
            self._open[key] = _Attached(network_name, holder_invocation_id, created)
        except BaseException:
            if room and network_name:
                await self._disconnect(room, network_name)
            if created and network_name:
                await self.manager._run_docker(("network", "rm", network_name), None, 30.0, None)
            raise
        finally:
            self._opening.discard(key)

    async def close(self, key: str) -> None:
        attached = self._open.get(key)
        if attached is None:
            return
        room = self.manager._known.get(key)
        if room and not await self._disconnect(room, attached.network_name):
            raise DirectTargetError("direct_target_disconnect_failed")
        if attached.created:
            code, _, _, timed_out, _ = await self.manager._run_docker(
                ("network", "rm", attached.network_name), None, 30.0, None
            )
            if code or timed_out:
                raise DirectTargetError("direct_target_network_remove_failed")
        self._open.pop(key, None)

    async def _disconnect(self, room: str, network_name: str) -> bool:
        code, _, _, timed_out, _ = await self.manager._run_docker(
            ("network", "disconnect", "-f", network_name, room), None, 30.0, None
        )
        return code == 0 and not timed_out

    async def _networkless(self, room: str) -> bool:
        code, output, _, timed_out, cut = await self.manager._run_docker(
            ("inspect", "--format", "{{json .NetworkSettings.Networks}}", room),
            None, 30.0, 65536,
        )
        if code or timed_out or cut:
            return False
        try:
            return json.loads(output) == {}
        except ValueError:
            return False


class ScopeValidator(Protocol):
    def __call__(
        self, target: AnyTarget, *, denials_only: bool = False
    ) -> tuple[bool, list[str], Any]: ...


class DirectTargetExecutionPolicy:
    def __init__(self, extension: DirectTargetNetworkExtension, scope_validator: ScopeValidator):
        self.extension = extension
        self._scope_validator = scope_validator

    def _failure(self, code: str) -> MCPCallResult:
        now = datetime.now(timezone.utc)
        return MCPCallResult(
            success=False,
            started_at=now,
            finished_at=now,
            output=None,
            error=ToolError(code=code, message="direct target execution refused: " + code,
                            retryable=False),
            metadata={"tool_name": SHELL_TARGET_RUN, "direct_target_refused": code},
        )

    def _validate(self, invocation: ToolInvocation) -> str | None:
        args = invocation.arguments
        if not isinstance(args, dict):
            return "direct_target_arguments_invalid"
        name, port = args.get("target_ref"), args.get("port")
        if not isinstance(name, str) or name not in self.extension.policy.targets:
            return "direct_target_unknown"
        if isinstance(port, bool) or not isinstance(port, int) or not 0 < port <= 65535:
            return "direct_target_port_required"
        selected = self.extension.policy.target_ports.get(name)
        if selected is not None and port not in selected:
            return "direct_target_port_unselected"
        approved = invocation.execution_context.approved_target
        if approved is None:
            return "direct_target_missing"
        identities, error = resolve_target_hostports(approved)
        if (
            error is not None or len(identities) != 1
            or identities[0][0].lower().rstrip(".") != name.lower().rstrip(".")
            or (identities[0][1] is not None and identities[0][1] != port)
        ):
            return "direct_target_approved_target_mismatch"
        try:
            allowed, _, _ = self._scope_validator(EndpointTarget(host=name, port=port))
        except Exception:
            return "direct_target_scope_error"
        if not allowed:
            return "direct_target_scope_denied"
        for address in self.extension.policy.targets[name]:
            try:
                allowed, _, _ = self._scope_validator(
                    EndpointTarget(host=address, port=port), denials_only=True
                )
            except Exception:
                return "direct_target_scope_error"
            if not allowed:
                return "direct_target_pin_denied"
        return None

    async def execute(self, invocation: ToolInvocation, parsed: Any, delegate) -> MCPCallResult:
        if not isinstance(parsed, dict) or not parsed.get("language") or not parsed.get("script"):
            return self._failure("direct_target_script_missing")
        refused = self._validate(invocation)
        if refused:
            return self._failure(refused)
        context = invocation.execution_context
        if context.instance_id is None:
            return self._failure("direct_target_instance_missing")
        try:
            key = workroom_key_for(str(context.run_id), str(context.instance_id))
            await self.extension.open(key, holder_invocation_id=str(invocation.invocation_id))
        except (DirectTargetError, ShellWorkroomError) as exc:
            return self._failure(str(exc) or "direct_target_open_failed")
        except Exception:
            return self._failure("direct_target_open_failed")
        try:
            result = await delegate()
        except BaseException:
            try:
                await asyncio.shield(self.extension.close(key))
            except Exception:
                pass
            raise
        try:
            await asyncio.shield(self.extension.close(key))
        except Exception:
            return self._failure("direct_target_disconnect_failed")
        metadata = dict(result.metadata or {})
        metadata["direct_target_network"] = "attached_for_execution"
        metadata["network_enforcement"] = "none"
        return replace(result, metadata=metadata)


def _descriptor(policy: ShellWorkroomPolicy, network: DirectTargetConfig) -> ToolDescriptor:
    descriptor = ToolDescriptor(
        tool_id=ToolId(SHELL_NAMESPACE + "." + SHELL_TARGET_RUN),
        server_id=SHELL_SERVER_ID,
        tool_name=SHELL_TARGET_RUN,
        description=(
            "Stateful Kali shell with direct Docker target networking. The workroom is attached "
            "to the selected route for this synchronous execution and detached afterwards. "
            "target_ref and port are checked against the investigation scope, but there is no "
            "packet allowlist, byte quota, flow quota, NFQUEUE, or sidecar enforcement."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "workroom_key": {
                    "type": "string", "minLength": 1, "maxLength": 128,
                    "pattern": WORKROOM_KEY_PATTERN,
                },
                "language": {"type": "string", "enum": list(SHELL_LANGUAGES)},
                "script": {
                    "type": "string", "minLength": 1,
                    "maxLength": policy.max_script_bytes,
                },
                "timeout_seconds": {
                    "type": "number", "exclusiveMinimum": 0,
                    "maximum": policy.max_timeout_seconds,
                },
                "target_ref": {
                    "type": "string", "enum": sorted(network.targets),
                },
                "port": {
                    "type": "integer", "minimum": 1, "maximum": 65535,
                    "enum": sorted({p for values in network.target_ports.values() for p in values}),
                },
            },
            "required": ["language", "script", "target_ref", "port"],
            "additionalProperties": False,
        },
        output_schema=dict(RUN_OUTPUT_SCHEMA),
        operations=[TARGET_RUN_OPERATION, RUN_OPERATION],
        target_types=list(TARGET_TYPES),
        capability_tags=[SHELL_NAMESPACE],
        risk_level=RiskLevel.MEDIUM,
        retry_safe=False,
        target_binding=ToolTargetBinding(binds_target_arguments=True),
        requires_target=True,
        command_fields=["language", "script"],
        availability_impact=False,
    )
    validate_tool_manifest(descriptor)
    return descriptor


def configure_direct_target(shell: ShellRuntime, scope, provider: DirectTargetProviderConfig):
    from functools import partial

    from redteam.core.scope_rules import check_target_scope

    extension = DirectTargetNetworkExtension(shell.adapter.program_source, provider)
    shell.adapter.program_source.set_execution_admission(extension)
    executor = DirectTargetExecutionPolicy(
        extension, partial(check_target_scope, scope=scope)
    )
    descriptor = _descriptor(shell.policy, provider.network)
    shell.adapter.set_network_policy(executor, [descriptor])
    return extension, executor, [descriptor]
