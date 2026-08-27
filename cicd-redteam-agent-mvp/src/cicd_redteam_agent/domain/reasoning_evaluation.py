from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from cicd_redteam_agent.domain.models import AnalysisReport, FindingStatus


@dataclass(frozen=True)
class ReasoningExpectation:
    """Fixture-only oracle used after analysis; never an analyzer input."""

    scenario_ids: tuple[str, ...]
    variant: str
    expected_status: FindingStatus
    required_relation_types: frozenset[str] = field(default_factory=frozenset)
    required_relation_verdicts: frozenset[tuple[str, str]] = field(
        default_factory=frozenset
    )
    path_relation_types: frozenset[str] = field(default_factory=frozenset)
    forbidden_relation_types: frozenset[str] = field(default_factory=frozenset)
    required_missing_evidence: frozenset[str] = field(default_factory=frozenset)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> ReasoningExpectation:
        identifiers = value.get("scenario_ids") or [value["scenario_id"]]
        required = value.get("required_relation_types")
        if required is None:
            required = [item[0] for item in value.get("required_relations", [])]
        required_verdicts = frozenset(
            (str(item[0]), str(item[1]))
            for item in value.get("required_relations", [])
        )
        return cls(
            scenario_ids=tuple(str(item) for item in identifiers),
            variant=str(value["variant"]),
            expected_status=FindingStatus(str(value["expected_status"])),
            required_relation_types=frozenset(required),
            required_relation_verdicts=required_verdicts,
            path_relation_types=frozenset(value.get("path_relations", [])),
            forbidden_relation_types=frozenset(value.get("forbidden_relation_types", [])),
            required_missing_evidence=frozenset(value.get("required_missing_evidence", [])),
        )


@dataclass(frozen=True)
class ReasoningEvaluation:
    scenario_ids: tuple[str, ...]
    variant: str
    extraction_complete: bool
    forbidden_absent: bool
    status_correct: bool
    missing_evidence_preserved: bool
    matched_path_ids: tuple[str, ...]
    missing_relation_types: tuple[str, ...]
    missing_relation_verdicts: tuple[str, ...]
    unexpected_relation_types: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return (
            self.extraction_complete
            and self.forbidden_absent
            and self.status_correct
            and self.missing_evidence_preserved
        )


def evaluate_reasoning_report(
    report: AnalysisReport, expectation: ReasoningExpectation
) -> ReasoningEvaluation:
    """Score an Agent result without exposing fixture labels or answers to the Agent."""

    extracted = {relation.relation_type for relation in report.state.relations}
    extracted_verdicts = {
        (relation.relation_type, relation.verdict.value)
        for relation in report.state.relations
    }
    missing_types = expectation.required_relation_types - extracted
    missing_verdicts = expectation.required_relation_verdicts - extracted_verdicts
    unexpected = expectation.forbidden_relation_types & extracted
    matching_paths = [
        path
        for path in report.state.paths
        if path.status is expectation.expected_status
        and expectation.path_relation_types.issubset(
            {edge.relation_type for edge in path.edges}
        )
    ]
    if expectation.expected_status is FindingStatus.REJECTED and not matching_paths:
        matching_paths = [
            path
            for path in report.state.paths
            if path.status is FindingStatus.REJECTED
        ]
    preserved = any(
        expectation.required_missing_evidence.issubset(path.missing_evidence)
        for path in matching_paths
    ) if expectation.required_missing_evidence else bool(matching_paths)
    return ReasoningEvaluation(
        scenario_ids=expectation.scenario_ids,
        variant=expectation.variant,
        extraction_complete=not missing_types and not missing_verdicts,
        forbidden_absent=not unexpected,
        status_correct=bool(matching_paths),
        missing_evidence_preserved=preserved,
        matched_path_ids=tuple(path.path_id for path in matching_paths),
        missing_relation_types=tuple(sorted(missing_types)),
        missing_relation_verdicts=tuple(
            f"{relation_type}:{verdict}"
            for relation_type, verdict in sorted(missing_verdicts)
        ),
        unexpected_relation_types=tuple(sorted(unexpected)),
    )
