"""6a: real central persistence/conditions with explicit offline provider doubles."""

from dataclasses import replace

import pytest

from redteam.attack_graph.service import AttackGraphService
from redteam.capability.development import DevelopmentResponse, DevelopmentToolGateway
from redteam.capability.servers.network import CapturedResponse
from redteam.orchestration.coordination import content_hash
from redteam.orchestration.specialist_tasks import HypothesisProbeProposal, register_probe_tasks
from redteam.orchestration.task_completion_contract import check_task_conditions
from tests.integration.test_central_completion import OriginalProtocolDouble
from tests.integration.test_central_development import HOST, TARGET, call, discover, gateway, turn
from tests.integration.test_plan4a_completion import originals_for
from tests.integration.test_probe_workers import ReviewedObservationModel, drive, selection

HTTP = "web.synthetic-http.http_request"


def provider(outputs):
    descriptor = gateway().catalog.all_descriptors()[0].model_copy(update={
        "tool_id": HTTP, "tool_name": "http_request", "server_id": "synthetic-http",
        "input_schema": {"type": "object", "required": ["host", "path", "target_ref"],
                         "properties": {key: {"type": "string"}
                                        for key in ("host", "path", "target_ref")}},
    })
    return OriginalProtocolDouble(DevelopmentToolGateway(
        [descriptor], [DevelopmentResponse(output=output) for output in outputs],
    ))


def response(session=None, principal=None, *, receipted=True, preview=False):
    output = {"target_ref": HOST, "session_ref": None,
              **CapturedResponse(200, {"content-type": "text/plain"}, b"observed",
                                 host=HOST, port=80, scheme="http",
                                 url=f"http://{HOST}/", has_session_receipt=receipted,
                                 actual_session_ref=session).summary(
                                     body_max_chars=32 if preview else 0,
                                 )}
    if principal:
        output["principal_ref"] = principal
    return output


async def observe(session, llm, outputs, *, subjects, criteria):
    state = session.deps.central_state
    mission = await state.get_mission(session.runtime.mission_id)
    graph = AttackGraphService(session.deps.graph_store)
    receipt = await register_probe_tasks(
        session.deps, proposal_id="principal-observation",
        proposal=HypothesisProbeProposal.model_validate({"tasks": [{
            "task_key": "observe", "title": "Observe response", "objective": "Inspect response",
            "role_id": "probe-worker", "target_node_ids": [TARGET], "principal_refs": subjects,
            "completion_criteria": criteria,
        }]}), expected_revision=mission.revision,
        expected_binding_hash=await state.transaction(lambda tx: tx.binding()[1], write=False),
        graph_read={TARGET: content_hash(await graph.get_node(TARGET))},
        known_targets={TARGET}, known_principals=set(subjects), max_tasks=1,
    )
    identity = receipt["task_ids"][0]
    llm.native_responses_by_task[identity] = [discover(HTTP), *[
        turn(call(f"response-{index}", HTTP, {"host": HOST, "path": "/",
                                             "target_ref": HOST}))
        for index in range(len(outputs))
    ]]
    outcome, = await drive(session)
    assert outcome.kind == "completed", outcome.error
    task = await state.get_task(identity)
    originals = await originals_for(session, outcome.runtime)
    assert originals, [message.get("content") for message in llm.last_native_request["messages"]
                       if message["role"] == "tool"]
    return task, outcome.runtime, originals


@pytest.mark.parametrize("preview", [False, True])
async def test_anonymous_original_passes_stored_conditions_and_independent_task_review(tmp_path,
                                                                                      preview):
    outputs = [response(preview=preview)]
    async with selection(tmp_path, tools=provider(outputs),
                         llm=ReviewedObservationModel()) as (session, llm, tools):
        task, runtime, originals = await observe(session, llm, outputs, subjects=["anonymous"],
                                                criteria=[{"kind": "observation",
                                                           "description": "Inspect response"}])
        assert await check_task_conditions(task, originals, deps=session.deps,
                                           run_id=str(runtime.run_id)) == []
        stored = await session.deps.evidence_store.get(originals[0])
        assert stored.metadata["http_observation"]["actual_session_ref"] is None
        assert stored.metadata["mapping_status"] == "mapped"
        assert "body_preview" not in stored.metadata["http_observation"]
        task = await session.deps.central_state.get_task(str(task.task_id))
        assert task.result.outcome == "confirmed", task.result.summary
        assert task.result.completion_receipt_id
        assert not await AttackGraphService(session.deps.graph_store).list_findings()
        assert (await session.deps.central_state.get_mission(runtime.mission_id)).status == "active"
        assert len(tools.inner.invocations) == 1


class EvidenceView:
    """Inject one inconsistent read for negative tests; never rewrite stored originals."""

    def __init__(self, inner, record):
        self.inner, self.record = inner, record

    async def get(self, identity):
        if identity == self.record.evidence_id:
            return self.record
        return await self.inner.get(identity)

    async def list_for_run(self, run_id):
        return await self.inner.list_for_run(run_id)


@pytest.mark.parametrize("case", ["missing", "echo", "conflict", "mock", "other_run"])
async def test_unproven_anonymous_original_is_rejected(tmp_path, case):
    output = response(receipted=case not in {"missing", "echo"})
    if case == "echo":
        output["principal_ref"] = "anonymous"
    async with selection(tmp_path, tools=provider([output])) as (session, llm, _):
        task, runtime, originals = await observe(session, llm, [output], subjects=["anonymous"],
                                                criteria=[{"kind": "observation",
                                                           "description": "Inspect response",
                                                           "principal_refs": ["anonymous"]}])
        deps = session.deps
        if case in {"conflict", "mock", "other_run"}:
            record = await session.deps.evidence_store.get(originals[0])
            meta = {**record.metadata}
            updates = {"metadata": meta}
            if case == "conflict":
                meta["http_observation"] = {"actual_session_ref": "unexpected"}
            if case == "mock":
                meta["mock"] = True
            if case == "other_run":
                updates["run_id"] = "run-another"
            deps = replace(deps, evidence_store=EvidenceView(
                deps.evidence_store, record.model_copy(update=updates),
            ))
        errors = await check_task_conditions(task, originals, deps=deps,
                                            run_id=str(runtime.run_id))
        assert errors
        if case == "conflict":
            assert any("receipts disagree" in error for error in errors)
        if case == "other_run":
            assert any("different run" in error for error in errors)


@pytest.mark.parametrize("named_proved", [True, False])
async def test_mixed_task_conditions_do_not_skip_account_proof(tmp_path, named_proved):
    outputs = [response(), response("s-alice", "alice", receipted=named_proved)]
    criteria = [{"kind": "observation", "description": "Anonymous response",
                 "principal_refs": ["anonymous"]},
                {"kind": "observation", "description": "Account response",
                 "principal_refs": ["alice"]}]
    async with selection(tmp_path, tools=provider(outputs)) as (session, llm, _):
        task, runtime, originals = await observe(session, llm, outputs,
                                                subjects=["anonymous", "alice"], criteria=criteria)
        errors = await check_task_conditions(task, originals, deps=session.deps,
                                            run_id=str(runtime.run_id))
        assert bool(errors) is not named_proved


async def test_declared_named_principal_without_actual_use_is_not_proof(tmp_path):
    outputs = [response("s-alice", "alice", receipted=False)]
    async with selection(tmp_path, tools=provider(outputs)) as (session, llm, _):
        task, runtime, originals = await observe(session, llm, outputs, subjects=["alice"],
                                                criteria=[{"kind": "observation",
                                                           "description": "Account response"}])
        record = await session.deps.evidence_store.get(originals[0])
        declared = record.model_copy(update={"metadata": {**record.metadata,
                                                         "principal_ref": "alice"}})
        deps = replace(session.deps, evidence_store=EvidenceView(session.deps.evidence_store,
                                                                declared))
        errors = await check_task_conditions(task, originals, deps=deps,
                                            run_id=str(runtime.run_id))
        assert any("declaration alone is not proof" in error for error in errors)


@pytest.mark.parametrize("second_proved", [True, False])
async def test_default_two_account_condition_requires_both_receipts(tmp_path, second_proved):
    outputs = [response("s-alice", "alice"), response("s-bob", "bob", receipted=second_proved)]
    async with selection(tmp_path, tools=provider(outputs)) as (session, llm, _):
        task, runtime, originals = await observe(session, llm, outputs, subjects=["alice", "bob"],
                                                criteria=[{"kind": "observation",
                                                           "description": "Compare responses"}])
        errors = await check_task_conditions(task, originals, deps=session.deps,
                                            run_id=str(runtime.run_id))
        assert bool(errors) is not second_proved
