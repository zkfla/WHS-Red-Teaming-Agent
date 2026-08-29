from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from pydantic import Field

from cicd_redteam_agent.domain.evidence_requirement import EvidenceRequirement
from cicd_redteam_agent.domain.investigation import (
    ObservationSource,
    RedTeamSession,
    RuntimeTelemetryEvent,
    RuntimeTelemetryEvidence,
    SchemaModel,
    SecretRotationPolicyEvidence,
)
from cicd_redteam_agent.domain.models import EvidenceProvider, EvidenceRequirementKind
from cicd_redteam_agent.framework.evidence_resolver import (
    EvidenceResolutionError,
    ResolutionFailureKind,
    ResolverDescriptor,
)

_MAX_TELEMETRY_BYTES = 4 * 1024 * 1024
_MAX_TELEMETRY_RECORDS = 2000


class RuntimeTelemetryRecord(SchemaModel):
    run_id: str = Field(min_length=1, max_length=100)
    run_attempt: int = Field(default=1, ge=1)
    source_name: str = Field(min_length=1, max_length=100)
    event: RuntimeTelemetryEvent


@dataclass(frozen=True)
class LocalRuntimeTelemetryResolver:
    path: Path
    untrusted_repository_root: Path

    observation_source = ObservationSource.LOCAL
    descriptor = ResolverDescriptor(
        resolver_id="local.runtime-telemetry.v1",
        provider=EvidenceProvider.LOCAL,
        supported_kinds=frozenset({EvidenceRequirementKind.RUNTIME_TELEMETRY}),
        max_calls=1,
        timeout_seconds=5.0,
    )

    def __post_init__(self) -> None:
        path = self.path.expanduser().resolve(strict=True)
        root = self.untrusted_repository_root.expanduser().resolve(strict=True)
        if not path.is_file():
            raise ValueError("runtime telemetry input must be a regular file")
        try:
            path.relative_to(root)
        except ValueError:
            pass
        else:
            raise ValueError(
                "runtime telemetry must come from a trusted operator path outside the "
                "analyzed repository"
            )
        if path.stat().st_size > _MAX_TELEMETRY_BYTES:
            raise ValueError("runtime telemetry exceeds the 4 MiB input limit")
        object.__setattr__(self, "path", path)
        object.__setattr__(self, "untrusted_repository_root", root)

    async def resolve(
        self,
        session: RedTeamSession,
        requirement: EvidenceRequirement,
    ) -> RuntimeTelemetryEvidence:
        if requirement.kind is not EvidenceRequirementKind.RUNTIME_TELEMETRY:
            raise EvidenceResolutionError(
                ResolutionFailureKind.UNSUPPORTED,
                f"local telemetry resolver does not support {requirement.kind.value}",
            )
        try:
            text = self.path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise EvidenceResolutionError(
                ResolutionFailureKind.TRANSIENT,
                f"runtime telemetry could not be read: {type(exc).__name__}",
            ) from exc
        records: list[RuntimeTelemetryRecord] = []
        for line_number, raw_line in enumerate(text.splitlines(), 1):
            if not raw_line.strip():
                continue
            if len(records) >= _MAX_TELEMETRY_RECORDS:
                raise EvidenceResolutionError(
                    ResolutionFailureKind.BUDGET_EXHAUSTED,
                    f"runtime telemetry exceeds {_MAX_TELEMETRY_RECORDS} records",
                )
            try:
                payload = json.loads(raw_line)
                records.append(RuntimeTelemetryRecord.model_validate(payload))
            except (json.JSONDecodeError, ValueError) as exc:
                raise EvidenceResolutionError(
                    ResolutionFailureKind.TRANSIENT,
                    f"invalid runtime telemetry JSONL record at line {line_number}",
                ) from exc
        if not records:
            raise EvidenceResolutionError(
                ResolutionFailureKind.NOT_APPLICABLE,
                "runtime telemetry input contains no records",
            )
        run_keys = {(item.run_id, item.run_attempt, item.source_name) for item in records}
        if len(run_keys) != 1:
            raise EvidenceResolutionError(
                ResolutionFailureKind.AMBIGUOUS_NOT_FOUND,
                "one telemetry evidence file must describe exactly one run/attempt/source",
            )
        run_id, run_attempt, source_name = next(iter(run_keys))
        return RuntimeTelemetryEvidence(
            run_id=run_id,
            run_attempt=run_attempt,
            source_name=source_name,
            events=[item.event for item in records],
        )


@dataclass(frozen=True)
class LocalSecretRotationPolicyResolver:
    max_age_days: int

    observation_source = ObservationSource.LOCAL
    descriptor = ResolverDescriptor(
        resolver_id="local.secret-rotation-policy.v1",
        provider=EvidenceProvider.LOCAL,
        supported_kinds=frozenset({EvidenceRequirementKind.OPERATOR_SECRET_ROTATION_POLICY}),
        max_calls=1,
        timeout_seconds=1.0,
    )

    def __post_init__(self) -> None:
        if self.max_age_days <= 0 or self.max_age_days > 3650:
            raise ValueError("secret rotation max age must be between 1 and 3650 days")

    async def resolve(
        self,
        session: RedTeamSession,
        requirement: EvidenceRequirement,
    ) -> SecretRotationPolicyEvidence:
        if requirement.kind is not EvidenceRequirementKind.OPERATOR_SECRET_ROTATION_POLICY:
            raise EvidenceResolutionError(
                ResolutionFailureKind.UNSUPPORTED,
                f"secret rotation policy resolver does not support {requirement.kind.value}",
            )
        return SecretRotationPolicyEvidence(max_age_days=self.max_age_days)
