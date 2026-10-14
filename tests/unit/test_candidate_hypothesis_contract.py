"""The active candidate update schema is v2-only; Hypothesis stays read-compatible."""

import pytest
from pydantic import ValidationError

from redteam.core.contracts.graph import CandidateHypothesis, Hypothesis, HypothesisUpdate


def _candidate(**overrides):
    candidate = {
        "contract_version": 2,
        "hypothesis_id": "candidate-local",
        "description": "Check whether the service exposes the intended behavior.",
        "target_node_ids": ["target-node"],
        "reason": "The target is in scope for this investigation.",
    }
    candidate.update(overrides)
    return candidate


def test_update_schema_publishes_required_v2_candidate_contract():
    schema = HypothesisUpdate.model_json_schema()
    hypothesis_ref = schema["properties"]["hypothesis"]["$ref"].rsplit("/", 1)[-1]
    candidate_schema = schema["$defs"][hypothesis_ref]
    properties = candidate_schema["properties"]

    assert {"contract_version", "target_node_ids", "reason"} <= set(
        candidate_schema["required"]
    )
    version_schema = properties["contract_version"]
    assert version_schema.get("const", version_schema.get("enum")) in (2, [2])
    assert "default" not in version_schema
    assert properties["target_node_ids"]["minItems"] == 1
    assert properties["reason"]["minLength"] == 1
    assert properties["status"]["enum"] == [
        "open", "confirmed", "rejected", "inconclusive", "held",
    ]
    assert properties["confidence"]["minimum"] == 0
    assert properties["confidence"]["maximum"] == 1


def test_historical_hypothesis_keeps_v1_round_trip_and_accepts_v2():
    legacy = {
        "hypothesis_id": "legacy-local",
        "description": "Historical record",
        "confidence": 1.5,
        "status": "refuted",
        "metadata": {"origin": "legacy"},
    }
    assert Hypothesis.model_validate(legacy).model_dump() == legacy

    current = Hypothesis.model_validate(_candidate(
        target_node_ids=["target-b", "target-a", "target-b"],
        principal_refs=["principal-b", "principal-a", "principal-b"],
    ))
    assert current.contract_version == 2
    assert current.target_node_ids == ["target-a", "target-b"]
    assert current.principal_refs == ["principal-a", "principal-b"]


@pytest.mark.parametrize("override", [
    {"target_node_ids": []},
    {"reason": "   "},
    {"status": "refuted"},
    {"confidence": -0.01},
    {"confidence": 1.01},
])
def test_candidate_hypothesis_rejects_values_outside_v2_contract(override):
    with pytest.raises(ValidationError):
        CandidateHypothesis.model_validate(_candidate(**override))


@pytest.mark.parametrize("version", [1, None])
def test_update_requires_explicit_v2_version(version):
    candidate = _candidate()
    if version is None:
        candidate.pop("contract_version")
    else:
        candidate["contract_version"] = version

    with pytest.raises(ValidationError) as failed:
        HypothesisUpdate.model_validate({"hypothesis": candidate})

    locations = [tuple(error["loc"]) for error in failed.value.errors()]
    assert ("hypothesis", "contract_version") in locations


def test_positive_candidate_verdict_needs_review_receipt():
    with pytest.raises(ValidationError) as failed:
        HypothesisUpdate.model_validate({"hypothesis": _candidate(status="confirmed")})

    assert failed.value.errors()[0]["type"] == "candidate_verdict_requires_review"
