"""Duplicate native observations use real receipts with offline model/provider doubles."""

import json

from redteam.capability.development import DevelopmentResponse, DevelopmentToolGateway
from redteam.core.contracts.tool import ToolDescriptor
from redteam.llm.fake import FakeLLMClient
from tests.integration.test_central_completion import OriginalProtocolDouble
from tests.integration.test_central_development import call, discover, turn
from tests.integration.test_probe_workers import drive, register, selection

SESSION_TOOL = "web.session_list"


def _inventory_gateway():
    descriptor = ToolDescriptor.model_validate({
        "tool_id": SESSION_TOOL,
        "server_id": "synthetic-web",
        "tool_name": "session_list",
        "description": "List synthetic configured session references.",
        "operations": ["response_analysis"],
        "capability_tags": ["web"],
        "risk_level": "low",
        "health_status": "healthy",
        "requires_target": False,
        "target_types": [],
        "input_schema": {"type": "object", "properties": {}},
    })
    inventory = {"sessions": [{
        "session_ref": "provisioned-ref",
        "target_ref": "unit.local",
        "principal_ref": "provisioned-principal",
    }]}
    return OriginalProtocolDouble(DevelopmentToolGateway(
        [descriptor], [DevelopmentResponse(output=inventory) for _ in range(2)],
    ))


async def test_unchanged_inventory_result_is_progress_context_not_auth_proof(tmp_path):
    llm = FakeLLMClient()
    async with selection(tmp_path, tools=_inventory_gateway(), llm=llm) as (
        session, llm, tools,
    ):
        task_id = await register(session, "repeated-inventory", actions=2)
        llm.native_responses_by_task[task_id] = [
            discover(SESSION_TOOL),
            turn(call("inventory-first", SESSION_TOOL, {})),
            turn(call("inventory-again", SESSION_TOOL, {})),
            turn(call("settle", "task.settle", {
                "outcome": "inconclusive",
                "reason": "Return the preserved inventory observation",
                "evidence_ids": [],
            })),
        ]

        outcome, = await drive(session)
        assert outcome.kind == "completed", outcome.error
        assert len(tools.inner.invocations) == 2

        native_requests = [
            request["messages"] for request in llm.requests
            if request["kind"] == "complete_native"
        ]

        def delivered(call_id):
            for messages in native_requests:
                for message in messages:
                    if (message.get("role") == "tool"
                            and message.get("tool_call_id") == call_id):
                        return json.loads(message["content"])
            raise AssertionError(f"missing delivered result for {call_id}")

        first = delivered("inventory-first")
        repeated = delivered("inventory-again")
        assert first["made_progress"] is True
        assert repeated["made_progress"] is False
        assert "original_results" in repeated
        execution = next(
            item for item in await session.deps.central_state.list_executions(task_id=task_id)
            if item.model_tool_call_id == "inventory-again"
        )
        stored = await session.deps.evidence_store.get(execution.result_evidence_id)
        assert stored is not None
        raw = stored.metadata["original_result"]["output"]
        assert raw["sessions"][0]["session_ref"] == "provisioned-ref"
        assert "actual_session_ref" not in raw["sessions"][0]

        usage = await session.deps.central_state.transaction(
            lambda tx: tx.probe_usage(task_id), write=False,
        )
        assert usage["no_progress"] == 1
