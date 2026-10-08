"""Preparation projections are not scope, Control, or target-observation receipts."""

import json
from copy import deepcopy
from dataclasses import replace

import pytest

from redteam.core.ports.llm import StructuredCompletion
from redteam.orchestration.context_projection import worker_proposal_preparation
from redteam.orchestration.delegated_runtime import decide_delegations
from redteam.orchestration.knowledge_validation import InternalAssessment, InternalTarget
from redteam.orchestration.paths.models import PathPlan, PathStep
from tests.unit.test_specialist_p2_reasoning import world


def preparation_fixture():
    step = PathStep(step_id="observe", objective="Observe an independently discovered endpoint",
                    target_node_ids=["endpoint:synthetic.invalid GET /public"],
                    required_fact_refs=["evidence:discovery"])
    path = PathPlan(path_id="revised", run_id="run", version=2, status="active",
                    reason="Observed new subject", goal="Bounded observation", steps=[step])
    target = InternalTarget(node_id=step.target_node_ids[0], node_type="endpoint",
                            origin="observed", verification="confirmed")
    material = InternalAssessment(run_id="run", targets={target.node_id: target},
                                  fact_refs={"evidence:discovery"})
    return path, step, material


@pytest.mark.parametrize("status", ["active", "accepted"])
def test_projects_registered_subject_and_revised_membership_without_permission_or_mutation(status):
    path, step, material = preparation_fixture()
    material.targets["unrelated"] = InternalTarget(node_id="unrelated")
    original = deepcopy((path, material))
    canonical = path.model_copy(update={"status": status}, deep=True)
    result = worker_proposal_preparation(path, step, canonical, material)
    assert result["path_binding"]["active_snapshot_matches"] is True
    assert result["path_binding"]["version"] == 2
    assert result["path_binding"]["readiness_errors"] == []
    assert result["targets"] == [{"node_id": step.target_node_ids[0],
                                  "registered_for_run": True, "origin": "observed",
                                  "verification": "confirmed"}]
    assert result["required_facts"] == [{"ref": "evidence:discovery", "grounded": True}]
    assert (path, material) == original
    assert "execution_allowed" not in result and "control_decision" not in result


@pytest.mark.parametrize("mutation", ["version", "run", "step", "status", "missing"])
def test_missing_stale_foreign_or_changed_path_is_not_accepted(mutation):
    path, step, material = preparation_fixture()
    canonical = path.model_copy(deep=True)
    if mutation == "version":
        canonical.version += 1
    elif mutation == "run":
        canonical.run_id = "foreign"
    elif mutation == "step":
        canonical.steps[0].target_node_ids = ["different-subject"]
    elif mutation == "status":
        canonical.status = "candidate"
    else:
        canonical = None
    result = worker_proposal_preparation(path, step, canonical, material)
    assert result["path_binding"]["active_snapshot_matches"] is False
    assert result["path_binding"]["readiness_errors"] is None


@pytest.mark.parametrize("failure", ["graph", "evidence", "foreign", "missing"])
def test_failed_or_foreign_knowledge_is_unknown_not_registration(failure):
    path, step, material = preparation_fixture()
    if failure == "graph":
        material.graph_lookup_failed = True
    elif failure == "evidence":
        material.evidence_lookup_failed = True
    elif failure == "foreign":
        material.run_id = "foreign"
    else:
        material = None
    result = worker_proposal_preparation(path, step, path, material)
    assert result["knowledge_lookup"] == "unavailable"
    assert result["targets"][0]["registered_for_run"] is None
    assert result["required_facts"][0]["grounded"] is None


def test_unknown_target_missing_fact_and_closed_step_are_not_reported_ready():
    path, step, material = preparation_fixture()
    material.targets.clear()
    material.fact_refs.clear()
    path.steps[0].status = "blocked"
    result = worker_proposal_preparation(path, step, path, material)
    assert result["targets"][0]["registered_for_run"] is False
    assert result["required_facts"][0]["grounded"] is False
    assert "step is not open for execution" in result["path_binding"]["readiness_errors"]
    assert "unconfirmed required fact: evidence:discovery" in (
        result["path_binding"]["readiness_errors"]
    )


@pytest.mark.parametrize("concurrency", [1, 2])
async def test_real_worker_consumer_gets_runtime_v2_preparation_before_control(
    tmp_path, concurrency,
):
    class PreparationModel:
        """Fixture consumer: no provider/target calls; does not invent an ALLOW."""
        def __init__(self):
            self.payloads = []
            self.host = ""

        async def complete_structured_with_usage(self, *, messages, **kwargs):
            payload, _ = json.JSONDecoder().raw_decode(messages[-1]["content"])
            self.payloads.append(payload)
            preparation = payload["proposal_preparation"]
            assert preparation["path_binding"]["active_snapshot_matches"] is True
            assert preparation["path_binding"]["version"] == 2
            assert preparation["path_binding"]["readiness_errors"] == []
            assert preparation["targets"][0]["registered_for_run"] is True
            assert not payload["runtime_observations"]
            assert payload["action_lifecycle"]["prior_control_allow_required_to_propose"] is False
            tool = payload["tools"][0]
            return StructuredCompletion({
                "decision": "propose_actions", "reason": "Submit for fresh Control admission",
                "actions": [{"target_node_id": preparation["targets"][0]["node_id"],
                             "tool_id": tool["tool_id"], "arguments": {"host": self.host},
                             "reason": "Bounded observation proposal"}],
            }, total_tokens=5)

    llm = PreparationModel()
    seeded, deps, gateway, _, path, runtime, _ = await world(tmp_path, llm)
    llm.host = seeded.host
    path = path.model_copy(update={"version": 2, "parent_version": 1})
    await deps.path_store.register(path, expected_version=1)
    for state in runtime.delegations.values():
        state.path_version = 2
    deps = replace(deps, settings=deps.settings.model_copy(update={
        "specialist_decision_concurrency": concurrency,
    }))
    from redteam.orchestration.engine import InvestigationEngine

    engine = InvestigationEngine(deps)
    engine._event_runtime = engine.updated_runtime = runtime
    context = await engine.context(runtime, await seeded.service.observe(step=0))
    assert context["action_lifecycle"]["control_allow_required_to_execute"] is True
    # A model/display claim cannot overwrite the fresh canonical preparation.
    context["proposal_preparation"] = {"execution_allowed": True}
    actions, _ = await decide_delegations(engine, path, runtime, context)
    assert len(llm.payloads) == 2
    assert len(actions) == 2, [
        state.unresolved for state in engine.updated_runtime.delegations.values()
    ]
    assert gateway.invocations == []
