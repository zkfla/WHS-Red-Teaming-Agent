"""Candidate update rejections expose stable rules without proposal contents."""

import pytest
from pydantic import ValidationError

from redteam.core.contracts.graph import HypothesisUpdate
from redteam.orchestration.diagnostics import diagnostic_for_exception


@pytest.mark.parametrize("hypothesis,rule", [
    ({"contract_version": 1, "hypothesis_id": "local",
      "description": "private-proposal-marker", "target_node_ids": ["target"],
      "reason": "public purpose"},
     "literal_error"),
    ({"contract_version": 2, "hypothesis_id": "local",
      "description": "private-proposal-marker", "target_node_ids": ["target"],
      "reason": "public purpose", "status": "confirmed"},
     "candidate_verdict_requires_review"),
])
def test_candidate_update_rule_is_safe_and_structured(hypothesis, rule):
    with pytest.raises(ValidationError) as failed:
        HypothesisUpdate.model_validate({"hypothesis": hypothesis})
    diagnostic = diagnostic_for_exception(
        failed.value, stage="hypothesis_planning",
        schema=HypothesisUpdate.model_json_schema(),
    )
    location = "$.hypothesis.contract_version" if rule == "literal_error" else "$"
    assert diagnostic.validation_issues == [{"rule": rule, "location": location}]
    assert "private-proposal-marker" not in diagnostic.model_dump_json()
