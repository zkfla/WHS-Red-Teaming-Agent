"""Worker-authored assessment drafts: retained assertions, never verdict authority."""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

from redteam.core.evidence_paths import REVIEW_FIELD_RULE, validate_raw_path
from redteam.core.redaction import redact_sensitive_text


def _safe_text(value):
    return redact_sensitive_text(value.strip()) if isinstance(value, str) else value


DraftText = Annotated[str, BeforeValidator(_safe_text), Field(min_length=1, max_length=4000)]
DraftRef = Annotated[str, Field(min_length=1, max_length=512, pattern=r"^\S+$")]


class FindingDraft(BaseModel):
    # Preserve the legacy recording schema while sharing its claim fields.
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    statement: str = Field(min_length=1)
    evidence_refs: list[str] = Field(default_factory=list)
    target_node_id: str | None = None
    hypothesis_id: str | None = None
    severity: str = "medium"
    principal_refs: list[str] = Field(
        default_factory=list,
        description=(
            "Accounts this vulnerability claim is about, if the claim "
            "names any. Optional: observations without an asserted account "
            "must not invent one. Each named account needs actual-use proof."
        ),
    )


class AssessmentTextSpan(BaseModel):
    """Character coordinates in one stored, redacted text field, not serialized JSON."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    offset: int = Field(ge=0, strict=True)
    length: int = Field(gt=0, strict=True)


class AssessmentEvidenceRef(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    evidence_id: DraftRef
    raw_path: list[StrictStr | StrictInt] = Field(
        default_factory=list, max_length=32, description=REVIEW_FIELD_RULE,
    )
    text_span: AssessmentTextSpan | None = None
    role: Literal["support", "refute", "context"] = "context"

    @field_validator("raw_path")
    @classmethod
    def _nonnegative_indices(cls, path):
        return validate_raw_path(path)


class AssessmentObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    statement: DraftText
    sources: list[AssessmentEvidenceRef] = Field(min_length=1, max_length=32)


class AssessmentExpectation(BaseModel):
    """Worker's declared boundary and its origin, not a code-certified restriction."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    statement: DraftText
    source: Literal["operator_provided", "evidence", "assumption"]
    evidence_refs: list[AssessmentEvidenceRef] = Field(default_factory=list, max_length=32)

    @model_validator(mode="after")
    def _evidence_origin(self):
        if self.source == "evidence" and not self.evidence_refs:
            raise ValueError("an evidence-derived expectation requires source references")
        return self


class AssessmentDraft(FindingDraft):
    """Immutable item within its owning Task result; all opinions are proposals."""

    contract_version: Literal[1] = 1
    draft_id: DraftRef
    statement: DraftText
    evidence_refs: list[DraftRef] = Field(default_factory=list, max_length=64)
    target_node_id: DraftRef
    hypothesis_id: DraftRef | None = None
    principal_refs: list[DraftRef] = Field(default_factory=list, max_length=32)
    name: DraftText
    vulnclass: DraftText
    judgment: Literal["supports", "refutes", "inconclusive"]
    reason: DraftText
    severity: DraftText = "medium"
    impact: DraftText | None = None
    expectation: AssessmentExpectation
    observations: list[AssessmentObservation] = Field(default_factory=list, max_length=32)
    limitations: list[DraftText] = Field(default_factory=list, max_length=32)
    alternative_explanations: list[DraftText] = Field(default_factory=list, max_length=32)
    limitation_refs: list[DraftRef] = Field(default_factory=list, max_length=64)

    def original_evidence_ids(self) -> list[str]:
        return list(dict.fromkeys([
            *self.evidence_refs,
            *(ref.evidence_id for item in self.observations for ref in item.sources),
            *(ref.evidence_id for ref in self.expectation.evidence_refs),
        ]))

    def all_evidence_ids(self) -> list[str]:
        return list(dict.fromkeys([*self.original_evidence_ids(), *self.limitation_refs]))

    def identity(self, result_id: str) -> dict[str, str]:
        """Bind the complete normalized draft; never hash a shortened display title."""
        payload = json.dumps(self.model_dump(mode="json"), sort_keys=True,
                             separators=(",", ":"), ensure_ascii=False)
        return {"result_id": result_id, "draft_id": self.draft_id,
                "content_hash": hashlib.sha256(payload.encode()).hexdigest()}

    def as_finding_draft(self) -> FindingDraft:
        """Lossless legacy claim adapter, without silently entering its 500-char receipt cap.

        Longer drafts remain storable. A later versioned review consumer must
        handle them explicitly before that path can review the complete claim.
        """
        if len(self.statement) > 500:
            raise ValueError("assessment_claim_requires_full_length_review")
        return FindingDraft(
            statement=self.statement, evidence_refs=self.original_evidence_ids(),
            target_node_id=self.target_node_id, hypothesis_id=self.hypothesis_id,
            severity=self.severity, principal_refs=self.principal_refs,
        )


class AssessmentDraftBinding(BaseModel):
    """Immutable identity of the Task-result draft that a review consumed."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    contract_version: Literal[1] = 1
    result_id: DraftRef
    draft_id: DraftRef
    content_hash: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class ReviewAdoption(BaseModel):
    """Detail explicitly adopted by the independent reviewer.

    This is model output, not promotion authority. Code binds it to the exact
    draft and checked sources before a receipt can carry it onward.
    """

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    contract_version: Literal[1] = 1
    title: DraftText
    statement: DraftText
    summary: DraftText
    vulnclass: DraftText
    severity: DraftText
    severity_reason: DraftText | None = None
    impact: DraftText | None = None
    limitations: list[DraftText] = Field(default_factory=list, max_length=32)


class ReviewedFindingDetail(ReviewAdoption):
    """Receipt/Finding projection of adopted detail and its exact lineage."""

    draft_binding: AssessmentDraftBinding
    evidence_refs: list[AssessmentEvidenceRef] = Field(default_factory=list, max_length=96)
