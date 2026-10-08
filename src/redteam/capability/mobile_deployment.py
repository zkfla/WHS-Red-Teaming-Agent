"""Operator-only Android diagnostic deployment; no device/LLM/scope mutations.

Reuses the existing engagement seed metadata and stdio MCP settings. Creating
files proves configuration consistency, not connected-device/app readiness.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import sys
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from redteam.capability.deployment import digest, encoded
from redteam.capability.servers.mobile import DeviceProfile, MobileDiagnosticsPolicy
from redteam.capability.settings import environment_prefix
from redteam.core.config import EngagementConfig, load_engagement_config

_REF = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$"


class MobileInstallation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    asset_ref: str = Field(pattern=_REF)
    serial: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,64}$")
    package_name: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+$")


class MobileDeploymentSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    version: Literal[1] = 1
    server_id: str = "mobile-diagnostics"
    devices: dict[str, MobileInstallation] = Field(min_length=1, max_length=30)
    additional_server_ids: list[str] = Field(default_factory=list, max_length=30)
    allowed_commands: list[str] = Field(default_factory=lambda: ["getprop", "dumpsys_battery"])
    adb_path: str = "adb"
    timeout_seconds: float = Field(default=10, gt=0, le=30)
    max_output_bytes: int = Field(default=8192, ge=1024, le=65536)

    @model_validator(mode="after")
    def consistent(self):
        ids = [self.server_id, *self.additional_server_ids]
        for server_id in ids:
            environment_prefix(server_id)
        if len(set(ids)) != len(ids):
            raise ValueError("mobile_deployment_duplicate_server")
        if any(re.fullmatch(_REF, ref) is None for ref in self.devices):
            raise ValueError("mobile_deployment_invalid_device_ref")
        for field in ("asset_ref", "serial"):
            values = [getattr(item, field) for item in self.devices.values()]
            if len(set(values)) != len(values):
                raise ValueError("mobile_deployment_ambiguous_installation")
        self.server_policy()  # Same command/path/limit validation as the server.
        return self

    def server_policy(self) -> MobileDiagnosticsPolicy:
        return MobileDiagnosticsPolicy(
            devices={ref: DeviceProfile(serial=item.serial) for ref, item in self.devices.items()},
            allowed_commands=self.allowed_commands,
            adb_path=self.adb_path,
            timeout_seconds=self.timeout_seconds,
            max_output_bytes=self.max_output_bytes,
        )


def deployment_metadata(spec: MobileDeploymentSpec, engagement: EngagementConfig) -> dict:
    assets = {item.asset_ref for item in spec.devices.values()}
    if assets - set(engagement.scope.allowed_assets) or assets & set(
        engagement.scope.denied_assets
    ):
        raise ValueError("mobile_deployment_asset_outside_scope")
    result = {}
    for singular, field in (
        ("device_ref", None),
        ("device_serial", "serial"),
        ("package_name", "package_name"),
    ):
        declared = {
            item.asset_ref: ref if field is None else getattr(item, field)
            for ref, item in spec.devices.items()
        }
        # Do not let a stale single-asset shorthand disagree with the plural
        # metadata which seed_from_engagement actually consumes.
        old_singular = engagement.metadata.get(singular)
        if old_singular is not None and (
            len(engagement.scope.allowed_assets) != 1
            or len(declared) != 1
            or old_singular != next(iter(declared.values()))
        ):
            raise ValueError("mobile_deployment_existing_metadata_conflicts")
        key = singular + "s"
        old = engagement.metadata.get(key, {})
        if not isinstance(old, dict) or any(
            ref in old and old[ref] != value for ref, value in declared.items()
        ):
            raise ValueError("mobile_deployment_existing_metadata_conflicts")
        result[key] = {**old, **declared}
    return result


def connection_environment(
    spec: MobileDeploymentSpec, policy_path: str, policy_digest: str
) -> dict:
    prefix = environment_prefix(spec.server_id)
    return {
        "REDTEAM_MCP_SERVERS": ",".join([*spec.additional_server_ids, spec.server_id]),
        f"{prefix}_KIND": "mobile",
        f"{prefix}_TRANSPORT": "stdio",
        f"{prefix}_COMMAND": sys.executable,
        f"{prefix}_ARGS_JSON": json.dumps(
            [
                "-m",
                "redteam.capability.servers.mobile",
                "--policy",
                policy_path,
                "--policy-sha256",
                policy_digest,
            ]
        ),
        f"{prefix}_RISK_LEVEL": "medium",
        f"{prefix}_MAX_CONCURRENCY": "1",
        # Permit the bounded structured envelope as well as escaped output.
        f"{prefix}_MAX_OUTPUT_BYTES": str(max(1024, spec.max_output_bytes * 8 + 2048)),
        f"{prefix}_PASSTHROUGH_ENV": "",
        f"{prefix}_ALLOWED_TOOLS_JSON": json.dumps(["device_shell"]),
        f"{prefix}_TOOL_OPERATIONS_JSON": json.dumps({"device_shell": ["device_shell"]}),
    }


def provision(spec: MobileDeploymentSpec, engagement: EngagementConfig, output: Path) -> Path:
    """Create new private files only; preserve ROE/budgets/approval/default profiles."""
    public = deployment_metadata(spec, engagement)
    if "mobile_deployment" in engagement.metadata:
        raise ValueError("mobile_deployment_already_bound")
    output = output.absolute()
    policy_path = output / "policy.json"
    policy_bytes = encoded(spec.server_policy().model_dump(mode="json"))
    public["mobile_deployment"] = {
        "source_sha256": digest(encoded(spec.model_dump(mode="json"))),
        "policy_path": str(policy_path),
        "policy_sha256": digest(policy_bytes),
        "verification": "operator_declared_not_device_or_app_verified",
    }
    updated = engagement.model_copy(update={"metadata": {**engagement.metadata, **public}})
    env = connection_environment(spec, str(policy_path), digest(policy_bytes))
    files = {
        "deployment.json": encoded(spec.model_dump(mode="json")),
        "policy.json": policy_bytes,
        "engagement.yaml": yaml.safe_dump(
            updated.model_dump(mode="json"), sort_keys=False
        ).encode(),
        "framework.env": (
            "\n".join(f"export {key}={shlex.quote(value)}" for key, value in env.items()) + "\n"
        ).encode(),
    }
    output.mkdir(mode=0o700, parents=False, exist_ok=False)
    for name, content in files.items():
        fd = os.open(output / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
    return output / "engagement.yaml"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", required=True)
    parser.add_argument("--engagement", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    spec = MobileDeploymentSpec.model_validate(json.loads(Path(args.spec).read_text()))
    result = provision(spec, load_engagement_config(args.engagement), Path(args.output))
    print(f"Prepared {result}; device/app access has not been verified.")


if __name__ == "__main__":
    main()
