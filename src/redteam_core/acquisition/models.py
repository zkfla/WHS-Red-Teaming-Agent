from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from redteam_core.models.base import SchemaModel, stable_id
from redteam_core.models.provenance import TYPE_ID_PATTERN


class AcquisitionLimits(SchemaModel):
    max_scanned_files: int = Field(default=5_000, ge=1, le=1_000_000)
    max_total_bytes_read: int = Field(default=20_000_000, ge=1, le=2_000_000_000)
    max_single_file_bytes: int = Field(default=2_000_000, ge=1, le=1_000_000_000)
    max_inventory_results: int = Field(default=1_000, ge=1, le=100_000)
    max_search_results: int = Field(default=200, ge=1, le=10_000)
    max_read_characters: int = Field(default=40_000, ge=1, le=5_000_000)
    max_snippet_characters: int = Field(default=500, ge=1, le=10_000)


class AcquisitionBudget(SchemaModel):
    limits: AcquisitionLimits = Field(default_factory=AcquisitionLimits)
    scanned_files: int = Field(default=0, ge=0)
    bytes_read: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_counters(self) -> AcquisitionBudget:
        if self.scanned_files > self.limits.max_scanned_files:
            raise ValueError("acquisition scanned-file budget exceeded")
        if self.bytes_read > self.limits.max_total_bytes_read:
            raise ValueError("acquisition byte budget exceeded")
        return self

    def consume(self, *, scanned_files: int = 0, bytes_read: int = 0) -> None:
        if scanned_files < 0 or bytes_read < 0:
            raise ValueError("acquisition budget consumption cannot be negative")
        next_files = self.scanned_files + scanned_files
        next_bytes = self.bytes_read + bytes_read
        if next_files > self.limits.max_scanned_files:
            raise AcquisitionBudgetExceeded("acquisition scanned-file budget exceeded")
        if next_bytes > self.limits.max_total_bytes_read:
            raise AcquisitionBudgetExceeded("acquisition byte budget exceeded")
        self.scanned_files = next_files
        self.bytes_read = next_bytes


class ArtifactEntry(SchemaModel):
    relative_path: str = Field(min_length=1, max_length=4_000)
    size_bytes: int = Field(ge=0)


class ArtifactInventory(SchemaModel):
    entries: tuple[ArtifactEntry, ...]
    truncated: bool = False


class SearchHit(SchemaModel):
    relative_path: str = Field(min_length=1, max_length=4_000)
    line_number: int = Field(ge=1)
    snippet: str = Field(max_length=10_000)
    content_trust: Literal["UNTRUSTED_ARTIFACT"] = "UNTRUSTED_ARTIFACT"


class ArtifactSearchResult(SchemaModel):
    query: str = Field(min_length=1, max_length=1_000)
    hits: tuple[SearchHit, ...]
    skipped_oversize_files: tuple[str, ...] = ()
    truncated: bool = False


class ArtifactReadResult(SchemaModel):
    relative_path: str = Field(min_length=1, max_length=4_000)
    content: str = Field(max_length=5_000_000)
    size_bytes: int = Field(ge=0)
    truncated: bool = False
    content_trust: Literal["UNTRUSTED_ARTIFACT"] = "UNTRUSTED_ARTIFACT"


class AnalysisHint(SchemaModel):
    hint_id: str = Field(pattern=r"^hint_[0-9a-f]{16}$")
    hint_type_id: str = Field(pattern=TYPE_ID_PATTERN)
    subject_ref: str = Field(min_length=1, max_length=1_000)
    score: int = Field(ge=0, le=1_000)
    reason: str = Field(min_length=1, max_length=1_000)
    grounding_refs: tuple[str, ...] = ()
    advisory_only: Literal[True] = True

    @classmethod
    def create(
        cls,
        *,
        hint_type_id: str,
        subject_ref: str,
        score: int,
        reason: str,
        grounding_refs: tuple[str, ...] = (),
    ) -> AnalysisHint:
        return cls(
            hint_id=stable_id("hint", hint_type_id, subject_ref, reason),
            hint_type_id=hint_type_id,
            subject_ref=subject_ref,
            score=score,
            reason=reason,
            grounding_refs=grounding_refs,
        )


class AcquisitionError(RuntimeError):
    pass


class ArtifactBoundaryViolation(AcquisitionError):
    pass


class ArtifactOversizeError(AcquisitionError):
    pass


class AcquisitionBudgetExceeded(AcquisitionError):
    pass
