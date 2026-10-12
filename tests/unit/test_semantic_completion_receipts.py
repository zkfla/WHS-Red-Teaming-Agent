"""Semantic completion + review receipts through the real verification path.

The meaning-judgment seam, the single-action verification step, and the
code-only completion gate are exercised together: a scripted model (no
network) drives the real structured meaning verifier, every case runs the
real verify step, and completion is checked with the real completion
validator. The scripted model and the scripted evidence store below are
local to this file; no existing fakes or production code are touched.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from redteam.attack_graph.identity import node_id_for_target
from redteam.core.contracts.target import HostTarget
from redteam.core.ids import (
    AgentId,
    EngagementId,
    RunId,
)

NOW = datetime.now(timezone.utc)
RUN = RunId("run_receipts")
ENGAGEMENT = EngagementId("eng_receipts")
AGENT = AgentId("recon-agent")
HOST = "target.local"
TARGET = HostTarget(host=HOST)
TARGET_NODE = str(node_id_for_target(TARGET))
PATH_ID = "path_1"
PATH_VERSION = 1
STEP_ID = "step_1"
HYP_ID = "hyp_1"


































































# Runtime-local recording regressions use preserved originals and the real
# verifier, graph service and completion consumer; only the model is scripted.




















@pytest.mark.parametrize("segment", [True, 1.5])
def test_boolean_and_float_path_segments_are_rejected(segment):
    from pydantic import ValidationError

    from redteam.orchestration.graph_recording import GraphRecordingInput

    with pytest.raises(ValidationError) as caught:
        GraphRecordingInput.model_validate(
            {
                "facts": [
                    {
                        "evidence_id": "ev-1",
                        "raw_path": ["items", segment],
                        "fact_name": "first_name",
                        "target_node_id": "host:target.local",
                    }
                ]
            }
        )
    assert any("raw_path" in item["loc"] for item in caught.value.errors())


def test_published_pointer_rule_is_shared_by_schema_and_descriptor():
    from redteam.orchestration.graph_recording import (
        GraphRecordingInput,
        graph_recording_descriptor,
    )

    schema = GraphRecordingInput.model_json_schema()
    field = schema["$defs"]["RecordedFact"]["properties"]["raw_path"]
    marker = "envelope-prefixed"
    assert marker in field["description"]
    assert marker in graph_recording_descriptor().description






























# A rejected recording input publishes the same shared validation diagnostic
# as the native-loop helpers, scoped to the recording schema itself.
