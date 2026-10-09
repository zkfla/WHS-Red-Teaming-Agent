"""Recovery uses the registered owner and preserved invocation identity."""

from datetime import datetime, timezone

import pytest

from redteam.capability.bootstrap import build_mcp_tool_gateway
from redteam.core.contracts.execution import (
    ExecutionRecoveryAnswer,
    ExecutionReference,
    ExecutionState,
)
from redteam.core.contracts.target import EndpointTarget
from redteam.core.contracts.tool import ToolResult
from redteam.core.convert import digest_payload, execution_input_digest
from redteam.core.ids import ActionId
from tests.unit.test_builtin_provider_gateway import DurableContextProvider, invocation


class RecoverableProvider(DurableContextProvider):
    def __init__(self):
        super().__init__()
        self.prepared = []
        self.queries = []
        self.original = None

    def prepare_reference(self, call):
        self.prepared.append(call)
        return ExecutionReference(
            engagement_id=call.execution_context.engagement_id, run_id=call.run_id,
            instance_id=call.execution_context.instance_id, action_id=call.action_id,
            invocation_id=call.invocation_id, tool_id=call.tool_id,
            input_digest=execution_input_digest(call),
            provider_handle={"receipt": str(call.invocation_id)},
        )

    async def query_execution(self, reference):
        self.queries.append(reference)
        return ExecutionRecoveryAnswer(state=ExecutionState.RUNNING)

    async def fetch_execution(self, reference):
        self.queries.append(reference)
        return self.original


async def test_reference_is_bound_pure_and_routes_reads_without_invocation():
    provider = RecoverableProvider()
    gateway = await build_mcp_tool_gateway([], builtin_adapters=[provider])
    reference = gateway.prepare_reference(invocation({}))
    assert reference is not None
    assert provider.prepared[0].arguments == {"run_id": "run-test"}
    assert reference.input_digest == digest_payload({"run_id": "run-test"})
    assert provider.calls == []
    answer = await gateway.query_execution(reference)
    assert answer.state is ExecutionState.RUNNING and answer.uncertain
    assert await gateway.fetch_execution(reference) is None
    assert provider.calls == [] and provider.queries == [reference, reference]


async def test_wrong_context_and_forged_provider_identity_fail_before_entry():
    provider = RecoverableProvider()
    gateway = await build_mcp_tool_gateway([], builtin_adapters=[provider])
    with pytest.raises(ValueError, match="execution_reference_context_invalid"):
        gateway.prepare_reference(invocation({"run_id": "foreign"}))
    assert provider.prepared == [] and provider.calls == []
    prepare = provider.prepare_reference
    provider.prepare_reference = lambda call: prepare(call).model_copy(
        update={"action_id": ActionId("foreign")}
    )
    with pytest.raises(ValueError, match="execution_reference_identity_mismatch"):
        gateway.prepare_reference(invocation({}))
    assert provider.calls == []


async def test_preserved_original_returned_and_foreign_result_rejected():
    provider = RecoverableProvider()
    gateway = await build_mcp_tool_gateway([], builtin_adapters=[provider])
    call = invocation({})
    reference = gateway.prepare_reference(call)
    assert reference is not None
    now = datetime.now(timezone.utc)
    provider.original = ToolResult(
        invocation_id=call.invocation_id, action_id=call.action_id, tool_id=call.tool_id,
        success=True, output={"original": "preserved"}, started_at=now, finished_at=now,
    )
    assert await gateway.fetch_execution(reference) == provider.original
    provider.original = provider.original.model_copy(update={"action_id": ActionId("foreign")})
    with pytest.raises(ValueError, match="execution_recovery_result_identity_mismatch"):
        await gateway.fetch_execution(reference)
    assert provider.calls == []


async def test_provider_removed_is_unknown_without_rerun():
    provider = RecoverableProvider()
    gateway = await build_mcp_tool_gateway([], builtin_adapters=[provider])
    reference = gateway.prepare_reference(invocation({}))
    assert reference is not None
    empty = await build_mcp_tool_gateway([])
    answer = await empty.query_execution(reference)
    assert answer.state is ExecutionState.UNKNOWN and answer.uncertain
    assert answer.error is not None and answer.error.code == "execution_recovery_unavailable"
    assert await empty.fetch_execution(reference) is None
    assert provider.calls == []


async def test_approved_target_is_part_of_recoverable_input_digest():
    provider = RecoverableProvider()
    gateway = await build_mcp_tool_gateway([], builtin_adapters=[provider])
    call = invocation({})
    target = EndpointTarget(host="192.0.2.8", port=443)
    call = call.model_copy(update={"execution_context": call.execution_context.model_copy(update={
        "approved_target": target,
    })})
    reference = gateway.prepare_reference(call)
    assert reference is not None
    assert reference.input_digest == execution_input_digest(provider.prepared[0])
    foreign_context = call.execution_context.model_copy(update={
        "approved_target": EndpointTarget(host="192.0.2.9", port=443),
    })
    foreign = call.model_copy(update={"execution_context": foreign_context})
    changed = gateway.prepare_reference(foreign)
    assert changed is not None and changed.input_digest != reference.input_digest
    assert provider.calls == []
