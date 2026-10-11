"""Built-in discovery and trusted invocation identity use the public gateway."""

import asyncio
from datetime import datetime, timezone

import pytest

from redteam.capability.adapters.mcp.base import MCPCallResult
from redteam.capability.bootstrap import build_mcp_tool_gateway
from redteam.capability.settings import MCPConfigurationError
from redteam.core.contracts.tool import (
    EXECUTION_STARTED_METADATA_KEY,
    EXECUTION_UNCERTAIN_METADATA_KEY,
    ExecutionContext,
    ToolDescriptor,
    ToolInvocation,
)
from redteam.core.enums import ToolHealthStatus
from redteam.core.ids import ActionId, EngagementId, InvocationId, RunId, ToolId
from redteam.core.tool_binding import SELECTED_TOOL


class ContextProvider:
    server_id = "builtin-test"
    error_prefix = "builtin"

    def __init__(self):
        self.calls = []

    async def discover_tools(self):
        return [ToolDescriptor(
            tool_id=ToolId("builtin.inspect"), server_id=self.server_id,
            tool_name="inspect", requires_target=False,
            capability_tags=["builtin"],
            health_status=ToolHealthStatus.HEALTHY,
            input_schema={
                "type": "object",
                "properties": {"run_id": {"type": "string"}},
                "required": ["run_id"], "additionalProperties": False,
            },
        )]

    async def health_check(self):
        return ToolHealthStatus.HEALTHY

    def bind_context(self, invocation):
        if invocation.run_id != invocation.execution_context.run_id:
            raise ValueError("mismatched execution run")
        arguments = dict(invocation.arguments)
        expected = str(invocation.execution_context.run_id)
        if "run_id" in arguments and arguments["run_id"] != expected:
            raise ValueError("mismatched argument run")
        arguments["run_id"] = expected
        return arguments

    async def invoke(self, tool, arguments):
        self.calls.append(arguments)
        now = datetime.now(timezone.utc)
        return MCPCallResult(success=True, started_at=now, finished_at=now, output=arguments)


class DurableContextProvider(ContextProvider):
    async def invoke_contextual(self, invocation):
        self.calls.append(invocation)
        now = datetime.now(timezone.utc)
        return MCPCallResult(success=True, started_at=now, finished_at=now,
                             output=invocation.arguments)

    async def invoke(self, tool, arguments):
        raise AssertionError("contextual provider must receive trusted invocation identity")


class WaitingProvider(ContextProvider):
    def __init__(self):
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def invoke(self, tool, arguments):
        self.calls.append(arguments)
        self.entered.set()
        await self.release.wait()
        now = datetime.now(timezone.utc)
        return MCPCallResult(success=True, started_at=now, finished_at=now, output=arguments)


def invocation(arguments, *, context_run="run-test"):
    return ToolInvocation(
        invocation_id=InvocationId("inv-test"), action_id=ActionId("action-test"),
        run_id=RunId("run-test"), tool_id=ToolId("builtin.inspect"),
        binding_mode=SELECTED_TOOL, arguments=arguments,
        execution_context=ExecutionContext(
            engagement_id=EngagementId("eng-test"), run_id=RunId(context_run),
        ), created_at=datetime.now(timezone.utc),
    )


async def test_builtin_uses_catalog_and_admission_before_execution():
    provider = ContextProvider()
    gateway = await build_mcp_tool_gateway([], builtin_adapters=[provider])
    assert [str(tool.tool_id) for tool in gateway.all_descriptors()] == ["builtin.inspect"]
    admitted = []

    async def before_start():
        assert provider.calls == []
        admitted.append(True)

    result = await gateway.invoke_with_admission(invocation({}), before_start=before_start)
    assert result.success
    assert admitted == [True]
    assert provider.calls == [{"run_id": "run-test"}]


async def test_contextual_provider_receives_bound_identity_only_after_admission():
    provider = DurableContextProvider()
    gateway = await build_mcp_tool_gateway([], builtin_adapters=[provider])
    original = invocation({})

    async def before_start():
        assert provider.calls == []

    result = await gateway.invoke_with_admission(original, before_start=before_start)
    assert result.success
    supplied = provider.calls[0]
    assert supplied.invocation_id == original.invocation_id
    assert supplied.action_id == original.action_id
    assert supplied.execution_context == original.execution_context
    assert supplied.arguments == {"run_id": "run-test"}
    assert original.arguments == {}


async def test_provider_started_timeout_is_uncertain_and_cannot_retry():
    provider = WaitingProvider()
    gateway = await build_mcp_tool_gateway([], builtin_adapters=[provider])
    result = await gateway.invoke(invocation({}).model_copy(update={"timeout_seconds": 0.02}))
    assert provider.entered.is_set() and len(provider.calls) == 1
    assert result.error.code == "tool_timeout" and not result.error.retryable
    assert result.metadata[EXECUTION_STARTED_METADATA_KEY] is True
    assert result.metadata[EXECUTION_UNCERTAIN_METADATA_KEY] is True


async def test_queue_timeout_is_known_before_start_and_never_admits_second_call():
    provider = WaitingProvider()
    gateway = await build_mcp_tool_gateway([], builtin_adapters=[provider])
    first = asyncio.create_task(gateway.invoke(invocation({})))
    await asyncio.wait_for(provider.entered.wait(), timeout=1)
    admissions = []

    async def before_start():
        admissions.append(True)

    try:
        result = await gateway.invoke_with_admission(
            invocation({}).model_copy(update={
                "invocation_id": InvocationId("queued"), "timeout_seconds": 0.02,
            }), before_start=before_start,
        )
        assert result.error.code == "tool_timeout" and result.error.retryable
        assert result.metadata[EXECUTION_STARTED_METADATA_KEY] is False
        assert EXECUTION_UNCERTAIN_METADATA_KEY not in result.metadata
        assert admissions == [] and len(provider.calls) == 1
    finally:
        provider.release.set()
        await first


@pytest.mark.parametrize("arguments,context_run", [
    ({"run_id": "another-run"}, "run-test"), ({}, "another-run"),
])
async def test_context_mismatch_does_not_execute(arguments, context_run):
    provider = ContextProvider()
    gateway = await build_mcp_tool_gateway([], builtin_adapters=[provider])
    result = await gateway.invoke(invocation(arguments, context_run=context_run))
    assert not result.success
    assert result.error.code == "builtin_context_mismatch"
    assert provider.calls == []


async def test_bound_arguments_revalidate_descriptor_schema():
    provider = ContextProvider()
    gateway = await build_mcp_tool_gateway([], builtin_adapters=[provider])
    result = await gateway.invoke(invocation({"unexpected": True}))
    assert not result.success
    assert result.error.code == "builtin_invalid_arguments"
    assert provider.calls == []


async def test_duplicate_builtin_provider_registration_is_rejected():
    with pytest.raises(MCPConfigurationError, match="duplicate provider"):
        await build_mcp_tool_gateway([], builtin_adapters=[ContextProvider(), ContextProvider()])


async def test_builtin_concurrency_allows_independent_calls_to_progress():
    both_entered = asyncio.Event()

    class ConcurrentProvider(ContextProvider):
        async def invoke(self, tool, arguments):
            self.calls.append(arguments)
            if len(self.calls) == 2:
                both_entered.set()
            await both_entered.wait()
            now = datetime.now(timezone.utc)
            return MCPCallResult(success=True, started_at=now, finished_at=now, output={})

    provider = ConcurrentProvider()
    gateway = await build_mcp_tool_gateway(
        [], builtin_adapters=[provider], builtin_concurrency={provider.server_id: 2},
    )
    results = await asyncio.wait_for(
        asyncio.gather(gateway.invoke(invocation({})), gateway.invoke(invocation({}))),
        timeout=2,
    )
    assert all(result.success for result in results)
    assert len(provider.calls) == 2


async def test_builtin_serialized_output_limit_is_enforced():
    provider = ContextProvider()
    gateway = await build_mcp_tool_gateway(
        [], builtin_adapters=[provider], builtin_output_limits={provider.server_id: 2},
    )
    result = await gateway.invoke(invocation({}))
    assert not result.success
    assert result.error.code == "tool_output_too_large"
    assert result.metadata["max_output_bytes"] == 2
    assert result.output is None


async def test_binding_failure_returns_diagnostic_without_execution():
    class BrokenProvider(ContextProvider):
        def bind_context(self, invocation):
            raise TypeError("synthetic private provider data")

    provider = BrokenProvider()
    gateway = await build_mcp_tool_gateway([], builtin_adapters=[provider])
    result = await gateway.invoke(invocation({}))
    assert not result.success and provider.calls == []
    assert result.error.code == "builtin_context_binding_failed"
    assert result.error.details["exception_type"] == "TypeError"
    assert "private provider data" not in result.model_dump_json()


async def test_binding_does_not_require_a_diagnostic_prefix():
    class PlainProvider:
        server_id = "builtin-test"

        async def discover_tools(self):
            return await ContextProvider().discover_tools()

        async def health_check(self):
            return ToolHealthStatus.HEALTHY

        def bind_context(self, invocation):
            raise ValueError("mismatched trusted identity")

        async def invoke(self, tool, arguments):
            raise AssertionError("binding rejection must not execute")

    gateway = await build_mcp_tool_gateway([], builtin_adapters=[PlainProvider()])
    result = await gateway.invoke(invocation({}))
    assert not result.success
    assert result.error.code == "provider_context_mismatch"


async def test_application_assembles_builtin_before_control_and_catalog(tmp_path):
    from redteam.app.bootstrap import build_application
    from redteam.core.config import load_app_config
    from redteam.llm.fake import FakeLLMClient

    config = load_app_config("examples/app.yaml")
    provider = ContextProvider()
    _, deps = await build_application(
        config, llm=FakeLLMClient(), builtin_adapters=[provider],
        builtin_concurrency={provider.server_id: 2},
        evidence_dir=tmp_path / "evidence", audit_dir=tmp_path / "audit",
        approval_dir=tmp_path / "approvals",
    )
    assert deps.tool_catalog is deps.tool_gateway
    assert [str(tool.tool_id) for tool in deps.tool_catalog.all_descriptors()] == [
        "builtin.inspect",
    ]
    result = await deps.tool_gateway.invoke(invocation({}))
    assert result.success
    assert provider.calls == [{"run_id": "run-test"}]
