"""Domain-neutral candidate compilation and deterministic scoring."""

from redteam.orchestration.candidates.catalog import ToolCatalog
from redteam.orchestration.candidates.compiler import CandidateCompiler
from redteam.orchestration.candidates.models import (
    CandidateDraft,
    ExecutableCandidate,
    PriorityScore,
    PrioritySignals,
)
from redteam.orchestration.candidates.scoring import score_candidate

__all__ = [
    "ToolCatalog",
    "CandidateCompiler",
    "CandidateDraft",
    "ExecutableCandidate",
    "PrioritySignals",
    "PriorityScore",
    "score_candidate",
]
