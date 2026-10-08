"""Completion reviews retain constraints and cited content without planning noise."""

from copy import deepcopy
from types import SimpleNamespace

from redteam.orchestration.context_projection import (
    completion_review_context,
    review_path_reference,
)


def test_review_retains_constraints_and_cited_later_windows_without_mutation():
    context = {
        "engagement_constraints": {"scope": {"allowed_hosts": ["lab.local"]}},
        "operator_instructions": [{"instruction": "read only"}],
        "mission": {"operator_constraints": {"read_only": True}, "objective": "whole goal"},
        "evidence_read_contract": {"mode": "read_evidence"},
        "requested_evidence": [
            {"evidence_id": "proof", "status": "ok", "text": "later decisive observation"},
            {"evidence_id": "unrelated", "status": "ok", "text": "other investigation"},
        ],
        "tools": [{"description": "irrelevant tool catalogue" * 100}],
        "feedback": ["old unrelated error"],
        "execution_history": [{"operation": "other operation"}],
    }
    original = deepcopy(context)

    projected = completion_review_context(context, ["proof"])

    assert projected["engagement_constraints"] == context["engagement_constraints"]
    assert projected["operator_instructions"] == context["operator_instructions"]
    assert projected["operator_constraints"] == {"read_only": True}
    assert projected["requested_evidence"] == context["requested_evidence"][:1]
    assert projected["evidence_read_contract"] == context["evidence_read_contract"]
    assert all(key not in projected for key in ("tools", "feedback", "execution_history"))
    assert context == original
    projected["engagement_constraints"]["scope"]["allowed_hosts"].append("other.local")
    assert context == original


def test_review_projection_masks_secrets_at_the_existing_boundary():
    projected = completion_review_context(
        {"engagement_constraints": {"api_key": "SecretSentinel", "scope": "lab.local"}}, []
    )
    assert "SecretSentinel" not in str(projected)
    assert projected["engagement_constraints"]["scope"] == "lab.local"


def test_path_reference_keeps_identity_and_version_without_methods():
    path = SimpleNamespace(
        path_id="path", run_id="run", mission_id="mission", mission_task_id="task",
        version=3, status="active", steps=[{"objective": "unrelated future method"}],
    )
    reference = review_path_reference(path)
    assert reference == {
        "path_id": "path", "run_id": "run", "mission_id": "mission",
        "mission_task_id": "task", "version": 3, "status": "active",
    }


def test_related_recorded_state_keeps_partial_results_and_omission_diagnostics():
    context = {
        "knowledge": {
            "facts": [
                {"grounded_evidence_ids": ["proof"], "claim_sources": {"facts:status": {
                    "evidence_id": "proof", "origin": "observed",
                }}, "observed_facts": {"status": 200}},
                {"grounded_evidence_ids": ["other"], "observed_facts": {"other": 1}},
            ],
            "omitted_fact_node_ids": ["omitted"],
            "graph_diagnostics": {"lookup_failed": False},
            "evidence_diagnostics": {"lookup_failed": True},
            "provided": {"claims": ["unconfirmed"]},
            "hypotheses": {"items": ["unrelated question"]},
        },
        "local_tool_results": [
            {"result": {"refs": ["proof"], "ok": False,
                        "payload": {"accepted": [1], "refused": [2]}}},
            {"result": {"refs": ["other"], "ok": True, "payload": {"unrelated": True}}},
        ],
    }
    projected = completion_review_context(context, ["proof"])
    assert projected["knowledge"]["facts"] == context["knowledge"]["facts"][:1]
    assert projected["knowledge"]["omitted_fact_node_ids"] == ["omitted"]
    assert projected["knowledge"]["evidence_diagnostics"]["lookup_failed"] is True
    assert "provided" not in projected["knowledge"]
    assert "hypotheses" not in projected["knowledge"]
    assert projected["local_tool_results"] == context["local_tool_results"][:1]


def test_review_retains_only_cited_validity_and_its_meaning_without_mutating_context():
    context = {"evidence_validity": [
        {"evidence_id": "proof", "status": "unbound", "meaning": "No reuse binding"},
        {"evidence_id": "other", "status": "stale"},
        None,
    ]}
    original = deepcopy(context)
    projected = completion_review_context(context, ["proof"])
    assert projected["evidence_validity"] == [context["evidence_validity"][0]]
    projected["evidence_validity"][0]["status"] = "changed"
    assert context == original


def test_review_retains_cited_operational_outcomes_not_an_unrelated_approval():
    context = {
        "evidence": [
            {"evidence_id": "proof", "metadata": {"action_id": "action-proof"}},
            {"evidence_id": "other", "metadata": {"action_id": "action-other"}},
        ],
        "runtime_observations": [
            {"action_id": "action-proof", "kind": "control", "outcome": "allow",
             "api_key": "SecretSentinel"},
            {"action_id": "action-other", "kind": "control", "outcome": "allow"},
            {"kind": "operator", "outcome": "stop"},
        ],
    }
    original = deepcopy(context)
    projected = completion_review_context(context, ["proof"])
    assert len(projected["runtime_observations"]) == 1
    assert projected["runtime_observations"][0]["action_id"] == "action-proof"
    assert "SecretSentinel" not in str(projected)
    assert context == original
