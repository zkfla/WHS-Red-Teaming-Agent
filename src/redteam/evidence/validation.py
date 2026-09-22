"""Track B internal Evidence linkage validation.

The validator depends only on the frozen ``EvidenceStore`` port.  It checks
stored correlation fields against caller supplied context; it does not decide
whether a Finding is valid and it does not verify Artifact bytes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Final, TypeAlias, cast

from pydantic import TypeAdapter, ValidationError

from redteam.core.contracts.target import (
    AnyTarget,
    AssetTarget,
    DomainTarget,
    EndpointTarget,
    GraphNodeTarget,
    HostTarget,
    PrincipalTarget,
    ServiceTarget,
    TargetRef,
    UrlTarget,
    target_graph_node_id,
)
from redteam.core.ids import (
    ActionId,
    EngagementId,
    EvidenceId,
    HypothesisId,
    InvocationId,
    RunId,
)
from redteam.core.ports.evidence_store import EvidenceStore

_TARGET_ADAPTER: TypeAdapter[AnyTarget] = TypeAdapter(TargetRef)


class _NotRequested:
    pass


NOT_REQUESTED: Final = _NotRequested()
ExpectedValue: TypeAlias = str | None | _NotRequested


@dataclass(frozen=True, slots=True)
class ExpectedEvidenceContext:
    """Expected links. ``NOT_REQUESTED`` differs from explicit ``None``."""

    engagement_id: EngagementId | None | _NotRequested = NOT_REQUESTED
    run_id: RunId | None | _NotRequested = NOT_REQUESTED
    action_id: ActionId | None | _NotRequested = NOT_REQUESTED
    invocation_id: InvocationId | None | _NotRequested = NOT_REQUESTED
    hypothesis_id: HypothesisId | None | _NotRequested = NOT_REQUESTED
    target: AnyTarget | None | _NotRequested = NOT_REQUESTED
    require_execution_evidence: bool = False


class CheckStatus(str, Enum):
    PASSED = "passed"
    MISMATCH = "mismatch"
    MISSING = "missing"
    NOT_COMPARABLE = "not_comparable"


class EvidenceProblemCode(str, Enum):
    EVIDENCE_NOT_FOUND = "evidence_not_found"
    REQUIRED_LINK_MISSING = "required_link_missing"
    CONTEXT_MISMATCH = "context_mismatch"
    NOT_COMPARABLE = "not_comparable"
    EXECUTION_EVIDENCE_REQUIRED = "execution_evidence_required"


@dataclass(frozen=True, slots=True)
class EvidenceCheck:
    field: str
    status: CheckStatus
    problem_code: EvidenceProblemCode | None = None


@dataclass(frozen=True, slots=True)
class EvidenceValidationResult:
    evidence_id: EvidenceId
    found: bool
    checks: tuple[EvidenceCheck, ...] = field(default_factory=tuple)

    @property
    def passed(self) -> bool:
        return self.found and bool(self.checks) and all(
            check.status is CheckStatus.PASSED for check in self.checks
        )


class EvidenceLinkValidator:
    def __init__(self, store: EvidenceStore) -> None:
        self._store = store

    async def validate(
        self,
        evidence_id: EvidenceId,
        expected: ExpectedEvidenceContext,
    ) -> EvidenceValidationResult:
        evidence = await self._store.get(evidence_id)
        if evidence is None:
            return EvidenceValidationResult(
                evidence_id=evidence_id,
                found=False,
                checks=(
                    EvidenceCheck(
                        "evidence_id",
                        CheckStatus.MISSING,
                        EvidenceProblemCode.EVIDENCE_NOT_FOUND,
                    ),
                ),
            )

        checks: list[EvidenceCheck] = []
        for name in (
            "engagement_id",
            "run_id",
            "action_id",
            "invocation_id",
            "hypothesis_id",
        ):
            wanted = getattr(expected, name)
            if wanted is NOT_REQUESTED:
                continue
            actual = getattr(evidence, name)
            checks.append(_compare_link(name, actual, wanted))

        if expected.target is not NOT_REQUESTED:
            checks.append(_compare_target(evidence.metadata.get("target"), expected.target))

        if expected.require_execution_evidence:
            if evidence.invocation_id is None or evidence.artifact_ref is None:
                checks.append(
                    EvidenceCheck(
                        "execution_evidence",
                        CheckStatus.MISSING,
                        EvidenceProblemCode.EXECUTION_EVIDENCE_REQUIRED,
                    )
                )
            else:
                checks.append(EvidenceCheck("execution_evidence", CheckStatus.PASSED))

        return EvidenceValidationResult(evidence_id, True, tuple(checks))


def _compare_link(name: str, actual: object, wanted: object) -> EvidenceCheck:
    if actual == wanted:
        return EvidenceCheck(name, CheckStatus.PASSED)
    if actual is None and wanted is not None:
        return EvidenceCheck(
            name, CheckStatus.MISSING, EvidenceProblemCode.REQUIRED_LINK_MISSING
        )
    return EvidenceCheck(name, CheckStatus.MISMATCH, EvidenceProblemCode.CONTEXT_MISMATCH)


def _compare_target(stored: object, wanted: object) -> EvidenceCheck:
    if wanted is None:
        if stored is None:
            return EvidenceCheck("target", CheckStatus.PASSED)
        return EvidenceCheck(
            "target", CheckStatus.MISMATCH, EvidenceProblemCode.CONTEXT_MISMATCH
        )
    if stored is None or not isinstance(stored, dict):
        return EvidenceCheck(
            "target", CheckStatus.NOT_COMPARABLE, EvidenceProblemCode.NOT_COMPARABLE
        )
    try:
        actual: AnyTarget = _TARGET_ADAPTER.validate_python(stored)
    except ValidationError:
        return EvidenceCheck(
            "target", CheckStatus.NOT_COMPARABLE, EvidenceProblemCode.NOT_COMPARABLE
        )
    if _target_identity(actual) == _target_identity(cast(AnyTarget, wanted)):
        return EvidenceCheck("target", CheckStatus.PASSED)
    return EvidenceCheck("target", CheckStatus.MISMATCH, EvidenceProblemCode.CONTEXT_MISMATCH)


def _target_identity(target: AnyTarget) -> tuple[object, ...]:
    graph_node_id = target_graph_node_id(target)
    if graph_node_id is not None:
        return ("graph_node", str(graph_node_id))
    if isinstance(target, HostTarget):
        return ("host", target.host)
    if isinstance(target, DomainTarget):
        return ("domain", target.domain)
    if isinstance(target, UrlTarget):
        return ("url", target.url)
    if isinstance(target, EndpointTarget):
        return ("endpoint", target.host, target.port, target.path)
    if isinstance(target, ServiceTarget):
        return ("service", target.service_name, target.host)
    if isinstance(target, PrincipalTarget):
        return ("principal", str(target.principal_id))
    if isinstance(target, AssetTarget):
        return ("asset", str(target.asset_id))
    if isinstance(target, GraphNodeTarget):
        return ("graph_node", str(target.node_id))
    raise TypeError("unsupported target contract")
