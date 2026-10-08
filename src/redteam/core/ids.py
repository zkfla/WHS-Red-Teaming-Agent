"""Typed identifiers used across every Core Contract.

Plain ``str`` is not used directly for identifiers so that type checkers
catch mix-ups (e.g. passing a ``HypothesisId`` where a ``FindingId`` is
expected). ``NewType`` is chosen over a wrapper class deliberately: it is
zero-cost at runtime, serializes as a plain string in Pydantic models
(no custom validators needed), and gives mypy/pyright full nominal
distinction — enough rigor for this stage without extra machinery.
"""

from __future__ import annotations

import uuid
from typing import NewType

RunId = NewType("RunId", str)
EngagementId = NewType("EngagementId", str)
AgentId = NewType("AgentId", str)
AgentInstanceId = NewType("AgentInstanceId", str)
ActionId = NewType("ActionId", str)
InvocationId = NewType("InvocationId", str)
ToolId = NewType("ToolId", str)
EvidenceId = NewType("EvidenceId", str)
HypothesisId = NewType("HypothesisId", str)
FindingId = NewType("FindingId", str)
EventId = NewType("EventId", str)
ApprovalId = NewType("ApprovalId", str)
GraphNodeId = NewType("GraphNodeId", str)
GraphEdgeId = NewType("GraphEdgeId", str)
PrincipalId = NewType("PrincipalId", str)
AssetId = NewType("AssetId", str)
MissionId = NewType("MissionId", str)
TaskId = NewType("TaskId", str)
OperatorIntentId = NewType("OperatorIntentId", str)


def new_id(prefix: str) -> str:
    """Generate a prefixed unique id, e.g. ``run_3f9c1a2b7e91``."""
    return f"{prefix}_{uuid.uuid4().hex[:12]}"
