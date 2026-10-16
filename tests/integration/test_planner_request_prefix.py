"""Independent final Planner requests through the real assembly, using doubles only."""

import json
from datetime import datetime, timezone

import pytest

from redteam.core.contracts.mission import OperatorIntent, Provenance
from redteam.core.ports.llm import LLMUsage, StructuredCompletion
from redteam.orchestration.central_runtime import runtime_token
from redteam.orchestration.coordination import StateConflict
from redteam.orchestration.operator_intents.models import IntentRecord
from redteam.orchestration.specialist_planner import SpecialistPlanner
from tests.integration.test_central_development import TOOL, discover, inspect, turn
from tests.integration.test_hypothesis_planner import PlannerDouble, candidate, held
from tests.integration.test_planner_context_lookup import lookup
from tests.integration.test_probe_workers import PROFILE, drive, register, selection


class RequestDouble(PlannerDouble):
    def __init__(self, decisions):
        super().__init__(decisions)
        self.envelopes = []
        self.prior_request_counts = []

    async def complete_structured(self, *, messages, response_schema, system=None, **kwargs):
        properties = response_schema.get("properties", {})
        if "questions" not in properties:
            return await super().complete_structured(
                messages=messages, response_schema=response_schema, system=system, **kwargs,
            )
        # The default profile uses the split planner schema. Keep that real path
        # covered while the legacy profile continues through PlannerDouble's
        # single-stage schema and response handling.
        body = messages[-1]["content"].split("\n\nPrevious attempt failed")[0]
        payload = json.loads(body)
        await self._wait_response("complete_structured", messages)
        self.planner_inputs.append(payload)
        response = self.decisions.pop(0) if self.decisions else held()
        response = response(payload) if callable(response) else response
        response = dict(response)
        response.setdefault("questions", [])
        return response

    async def complete_structured_with_usage(
        self, *, messages, response_schema, system=None, **kwargs,
    ):
        self.prior_request_counts.append(len(self.requests))
        self.envelopes.append(json.loads(json.dumps({
            "system": system, "schema": response_schema, "messages": messages,
        })))
        value = await self.complete_structured(
            messages=messages, response_schema=response_schema, system=system, **kwargs,
        )
        # Deliberately distinguish unreported from an explicit zero cache hit.
        usage = None if len(self.envelopes) == 1 else LLMUsage(10, 2, 12, 0)
        return StructuredCompletion(value=value, usage=usage)


def initial_held(reason="Await prerequisites"):
    return {**held(reason), "updates": [candidate()]}


@pytest.mark.parametrize("profile", [PROFILE, PROFILE.with_name("native-probes-legacy.yaml")])
async def test_independent_passes_keep_prefix_and_receive_current_worker_and_operator_data(
    tmp_path, profile,
):
    llm = RequestDouble([
        initial_held("First public judgment"), held("Later public judgment"),
    ])
    async with selection(tmp_path, llm=llm, profile=profile) as (session, _, tools):
        planner = SpecialistPlanner(session.deps)
        for index in range(2):
            if index:
                await session.deps.central_state.append(IntentRecord(
                    run_id=session.runtime.run_id, correlation_id="changed-guidance",
                    intent=OperatorIntent(
                        intent_id="changed-guidance", mission_id=session.runtime.mission_id,
                        intent_type="guidance", instruction="Assess the newly observed premise",
                        created_at=datetime.now(timezone.utc), provenance=Provenance(source="test"),
                    ),
                ))
            identity = await register(session, f"observation-{index}")
            llm.native_responses_by_task[identity] = [
                discover(TOOL), turn(inspect(f"original-{index}", account=str(index))),
            ]
            result, = await drive(session)
            assert result.kind == "completed", result.error
            assert await planner.advance(session.runtime) == []

        first, second = llm.envelopes
        assert first["system"] == second["system"]
        assert first["schema"] == second["schema"]
        assert all([message["role"] for message in request["messages"]] == ["user"]
                   for request in llm.envelopes)
        before, after = llm.planner_inputs
        assert before != after
        assert before["input_sequence"] < after["input_sequence"]
        assert len(before["execution_history"]) == 1 and len(after["execution_history"]) == 2
        assert "Assess the newly observed premise" in json.dumps(after["operator_instructions"])
        assert [item["model_usage"]["spent"] for item in (before, after)] == (
            llm.prior_request_counts
        )
        assert after["model_usage"]["spent"] > before["model_usage"]["spent"]
        planner_usage = after["model_usage"]["by_stage"]["hypothesis_planning"]
        assert planner_usage["spent"] == 1
        assert planner_usage["usage"]["cache_read_tokens"] == {"known": 0, "unknown": 1}
        assert "by_task" not in after["model_usage"] and "by_owner" not in after["model_usage"]
        assert "context_lookup_result" not in before and "context_lookup_result" not in after
        assert before["remaining_budget"] > after["remaining_budget"]
        # Dynamic values occur only in user DATA, not the fixed instructions/schema.
        prefix = first["system"] + json.dumps(first["schema"])
        assert "changed-guidance" not in prefix and str(session.runtime.run_id) not in prefix
        assert len(tools.inner.invocations) == 2
        usage = await session.deps.central_state.request_ledger().snapshot(
            str(session.runtime.run_id),
        )
        assert usage["spent"] == llm.prior_request_counts[-1] + 1
        assert usage["by_stage"]["hypothesis_planning"]["spent"] == 2
        assert usage["by_stage"]["hypothesis_planning"]["usage"]["cache_read_tokens"] == {
            "known": 0, "unknown": 1,
        }


@pytest.mark.parametrize("profile", [PROFILE, PROFILE.with_name("native-probes-legacy.yaml")])
async def test_lookup_and_mixed_reply_repair_preserve_prefix_and_refresh_request_accounting(
    tmp_path, profile,
):
    request = lookup(kind="task", mode="list")
    if profile.name == "native-probes-legacy.yaml":
        # The legacy single-stage schema has updates/tasks, not split-stage questions.
        request = {key: value for key, value in request.items() if key != "questions"}
        request["updates"] = []
        request["tasks"] = []
    mixed = {**request, "reviewed_task_ids": ["not-read"]}
    llm = RequestDouble([request, mixed, initial_held()])
    async with selection(tmp_path, llm=llm, profile=profile) as (session, _, tools):
        assert await SpecialistPlanner(session.deps).advance(session.runtime) == []
        first, queried, repair = llm.envelopes
        assert first["system"] == queried["system"] == repair["system"]
        assert first["schema"] == queried["schema"] == repair["schema"]
        assert len(first["messages"]) == len(queried["messages"]) == len(repair["messages"]) == 1
        assert "Previous attempt failed" not in queried["messages"][0]["content"]
        assert "planner_lookup_cannot_commit" in repair["messages"][0]["content"]
        assert [item["model_usage"]["spent"] for item in llm.planner_inputs] == [0, 1, 2]
        assert [item["decision_budget"]["used"] for item in llm.planner_inputs] == [1, 2, 3]
        assert llm.planner_inputs[-1]["decision_budget"]["normal_used"] == 2
        assert llm.planner_inputs[-1]["decision_budget"]["repair_used"] == 1
        assert not tools.inner.invocations and await session.deps.central_state.list_tasks() == []
        usage = await session.deps.central_state.request_ledger().snapshot(
            str(session.runtime.run_id),
        )
        assert usage["spent"] == 3


@pytest.mark.parametrize("contract", ["planner", "evidence_fields"])
async def test_changed_code_request_contract_refuses_execution_but_preserves_original_reads(
    tmp_path, monkeypatch, contract,
):
    from redteam.core import evidence_paths
    from redteam.orchestration import hypothesis_planner

    llm = RequestDouble([])
    async with selection(tmp_path, llm=llm) as (session, _, tools):
        state = session.deps.central_state
        original = await state.get_mission(session.runtime.mission_id)
        # Same profile/prompt, changed code contract: custom profiles are covered too.
        owner, field = ((hypothesis_planner, "PLANNER_REQUEST_CONTRACT_VERSION")
                        if contract == "planner" else
                        (evidence_paths, "EVIDENCE_FIELD_CONTRACT_VERSION"))
        monkeypatch.setattr(owner, field, getattr(owner, field) + 1)
        with pytest.raises(StateConflict, match="central_runtime_original_binding_mismatch"):
            await runtime_token(session.deps, session.runtime, require_task=False)
        assert await state.get_mission(session.runtime.mission_id) == original
        assert not llm.envelopes and not tools.inner.invocations
