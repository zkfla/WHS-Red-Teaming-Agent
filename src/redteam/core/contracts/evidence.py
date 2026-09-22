"""EvidenceRecord — the append-only record of what was observed and why.

Owned by PM (schema) — Control Team owns the store in ``redteam.evidence``.
Never embed large binaries directly — use ``artifact_ref`` to point at
a file/external storage location instead.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from redteam.core.enums import EvidenceSourceType
from redteam.core.ids import ActionId, EngagementId, EvidenceId, HypothesisId, InvocationId, RunId


class EvidenceRecord(BaseModel):
    evidence_id: EvidenceId
    run_id: RunId
    engagement_id: EngagementId

    source_type: EvidenceSourceType
    source_id: str

    action_id: ActionId | None = None
    invocation_id: InvocationId | None = None
    hypothesis_id: HypothesisId | None = None

    content_type: str

    artifact_ref: str | None = None
    digest: str

    summary: str | None = None

    created_at: datetime

    metadata: dict[str, Any] = Field(default_factory=dict)
