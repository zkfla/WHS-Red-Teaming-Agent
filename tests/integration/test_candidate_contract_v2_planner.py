"""The active split planner publishes and admits v2 candidates using fake clients only."""

import json
from copy import deepcopy

import pytest

from redteam.attack_graph.service import AttackGraphService
from redteam.llm.fake import FakeLLMClient
from redteam.orchestration.cognition import DecisionFailure
from redteam.orchestration.specialist_planner import SpecialistPlanner
from tests.integration.test_central_development import TARGET
from tests.integration.test_probe_workers import PROFILE
from tests.integration.test_probe_workers import selection as probe_selection


class CapturingPlannerLLM(FakeLLMClient):
    """Capture real planner schemas and return fixed synthetic structured responses."""

    def __init__(self, *, plans=(), atoms=(), decisions=()):
        super().__init__()
        self.responses = {
            "plan": list(plans), "atom": list(atoms), "decision": list(decisions),
        }
        self.schemas = {}

    async def complete_structured(self, *, messages, response_schema, system=None, **kwargs):
        properties = response_schema.get("properties", {})
        if "questions" in properties:
            stage = "plan"
        elif "tasks" in properties and "updates" not in properties:
            stage = "atom"
        else:
            stage = "decision"
        self.schemas[stage] = deepcopy(response_schema)
        await self._wait_response("complete_structured", messages)
        response = self.responses[stage].pop(0)
        if callable(response):
            body = messages[-1]["content"].split("\n\nPrevious attempt failed")[0]
            response = response(json.loads(body))
        return deepcopy(response)


def _plan_with_candidate():
    return {
        "updates": [{
            "expected_revision": None,
            "hypothesis": {
                "contract_version": 2,
                "hypothesis_id": "local-candidate",
                "description": "Access may be unguarded",
                "target_node_ids": [TARGET],
                "reason": "Assess the preserved response",
            },
        }],
        "questions": [{
            "question_id": "question-local",
            "objective": "Inspect the candidate premise",
            "target_node_ids": [TARGET],
            "completion_criteria": [{
                "kind": "observation",
                "description": "Inspect the candidate response",
            }],
            "candidate_refs": ["local-candidate"],
        }],
        "reason": "One scoped observation can resolve the candidate uncertainty",
        "termination": "continue",
    }


def _atomize_new_candidate(payload):
    return {"tasks": [{
        "task_key": "candidate-probe",
        "question_id": "question-local",
        "objective": "Inspect the candidate premise",
        "title": "Candidate response probe",
        "role_id": "probe-worker",
        "target_node_ids": [TARGET],
        "completion_criterion_ids": [item["criterion_id"]
                                     for item in payload["questions"][0]["completion_criteria"]],
        "probe_limits": {"max_actions": 1, "max_model_requests": 4},
    }]}


def _resolve_schema(root, node):
    while "$ref" in node:
        reference = node["$ref"]
        assert reference.startswith("#/$defs/")
        node = root["$defs"][reference.removeprefix("#/$defs/")]
    return node


def _assert_v2_candidate_schema(response_schema):
    update = _resolve_schema(response_schema, response_schema["properties"]["updates"]["items"])
    candidate = _resolve_schema(response_schema, update["properties"]["hypothesis"])
    properties = candidate["properties"]

    assert properties["contract_version"]["const"] == 2
    assert properties["contract_version"].get("default") != 1
    assert {"contract_version", "target_node_ids", "reason"} <= set(candidate["required"])
    assert properties["target_node_ids"]["minItems"] >= 1
    assert properties["reason"]["minLength"] >= 1


@pytest.mark.asyncio
async def test_default_v3_and_legacy_planners_publish_v2_only_candidate_schemas(tmp_path):
    active_llm = CapturingPlannerLLM(
        plans=[_plan_with_candidate()], atoms=[_atomize_new_candidate],
    )
    async with probe_selection(tmp_path / "active", llm=active_llm) as (session, _, _):
        assert session.deps.specialist_policy.probe_atomization is True
        task_ids = await SpecialistPlanner(session.deps).advance(session.runtime)
        assert len(task_ids) == 1
        candidates = await AttackGraphService(session.deps.graph_store).list_hypotheses()
        assert len(candidates) == 1
        assert candidates[0].contract_version == 2
        task = await session.deps.central_state.get_task(task_ids[0])
        assert task.hypothesis_revisions == {candidates[0].hypothesis_id: 1}
        _assert_v2_candidate_schema(active_llm.schemas["plan"])

    legacy_llm = CapturingPlannerLLM(decisions=[{
        "updates": [],
        "tasks": [],
        "reason": "Wait for an authorized observation",
        "termination": "held",
        "resume_conditions": ["An authorized observation arrives"],
    }])
    async with probe_selection(
        tmp_path / "legacy",
        profile=PROFILE.with_name("native-probes-legacy.yaml"),
        llm=legacy_llm,
    ) as (session, _, _):
        assert session.deps.specialist_policy.probe_atomization is False
        assert await SpecialistPlanner(session.deps).advance(session.runtime) == []
        _assert_v2_candidate_schema(legacy_llm.schemas["decision"])


@pytest.mark.parametrize(
    ("contract_version", "expected_rule"),
    [(1, "literal_error"), (None, "missing")],
)
async def test_default_v3_rejects_non_v2_candidate_at_nested_contract_field(
    tmp_path, monkeypatch, contract_version, expected_rule,
):
    # Exercise the planner's own Pydantic callback, as the standard outer JSON
    # Schema gate would otherwise reject these malformed fake responses first.
    from redteam.orchestration import cognition

    monkeypatch.setattr(cognition, "_validate_schema", lambda **kwargs: None)
    hypothesis = {
        "hypothesis_id": "local-candidate",
        "description": "Access may be unguarded",
        "target_node_ids": [TARGET],
        "reason": "Assess the preserved response",
    }
    if contract_version is not None:
        hypothesis["contract_version"] = contract_version
    response = {
        "updates": [{"expected_revision": None, "hypothesis": hypothesis}],
        "questions": [],
        "reason": "Do not admit a candidate without the active contract version",
        "termination": "replan",
    }
    llm = CapturingPlannerLLM(plans=[response] * 7)  # decision_max_attempts (6) + initial
    async with probe_selection(tmp_path, llm=llm) as (session, _, _):
        with pytest.raises(DecisionFailure) as rejected:
            await SpecialistPlanner(session.deps).advance(session.runtime)

        diagnostic = rejected.value.diagnostic.last_cause or rejected.value.diagnostic
        assert diagnostic.error_type == "ValidationError"
        assert {
            (issue["location"], issue["rule"])
            for issue in diagnostic.validation_issues
        } >= {("$.updates[0].hypothesis.contract_version", expected_rule)}
        assert await AttackGraphService(session.deps.graph_store).list_hypotheses() == []
        assert await session.deps.central_state.list_tasks() == []
