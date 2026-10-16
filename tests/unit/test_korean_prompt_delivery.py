"""Trusted prompt delivery through the real metered provider boundary.

The provider below is an explicit offline double. These checks exercise the
same request ledger and prompt snapshot used by production, then inspect the
system text received at that provider boundary.
"""

import asyncio
from pathlib import Path

import pytest

from redteam.app.agent_profile import LoadedSpecialists, load_specialist_profile
from redteam.core.config import DEFAULT_SPECIALIST_PROFILE_PATH
from redteam.core.contracts.llm_policy import LLMRequestPolicy
from redteam.core.ports.llm import NativeCompletion, StructuredCompletion
from redteam.orchestration.coordination import coordinator_for
from redteam.orchestration.llm_requests import RunRequestLedger
from redteam.orchestration.metered_llm import RunMeteredLLM, llm_request_scope
from redteam.orchestration.prompts import instruction_scope
from tests.unit.test_orchestration_coordination import RUN, world

DEFAULT_STAGE_ROLES = (
    ("hypothesis_planning", "planner"),
    ("task_atomization", "probe-atomizer"),
    ("native_action", "probe-worker"),
    ("analysis_verify", "research-evidence"),
    ("code_generation", "code-author"),
    ("conversation_decide", "conversation"),
)
RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {"contract_marker": {"const": "schema-v1"}},
    "required": ["contract_marker"],
    "additionalProperties": False,
}
NATIVE_TOOLS = [{
    "name": "observe",
    "description": "Return a bounded observation.",
    "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
}]


class OfflineCapturingProvider:
    """Record provider requests without opening a network connection."""

    def __init__(self):
        self.calls = []

    async def complete_structured_with_usage(self, **kwargs):
        assert kwargs["num_retries"] == 0
        self.calls.append(("structured", kwargs))
        return StructuredCompletion(value={"contract_marker": "schema-v1"}, total_tokens=1)

    async def complete_native(self, **kwargs):
        assert kwargs["num_retries"] == 0
        self.calls.append(("native", kwargs))
        return NativeCompletion(tool_calls=(), text="offline response")


def _role_documents(loaded: LoadedSpecialists, role_id: str) -> tuple[str, str]:
    profile_dir = Path(DEFAULT_SPECIALIST_PROFILE_PATH).resolve().parent
    common = (profile_dir / loaded.profile.common.path).read_bytes().decode("utf-8")
    role = next(
        role for role in loaded.profile.roles
        if str(role.definition.agent_id) == role_id
    )
    return common, (profile_dir / role.prompt.path).read_bytes().decode("utf-8")


def _assert_delivered_once(loaded, call, stage, role_id, runtime_contract):
    system = call["system"]
    common_text, role_text = _role_documents(loaded, role_id)

    assert loaded.profile.stages[stage] == role_id
    binding = loaded.prompts.binding(stage)
    role = next(
        role for role in loaded.profile.roles
        if str(role.definition.agent_id) == role_id
    )
    assert binding.role_id == role_id
    assert binding.version == role.prompt.version
    assert binding.contract_version == role.prompt.contract_version
    assert system == loaded.prompts.compose(stage, runtime_contract)
    assert system.count(common_text) == 1
    assert system.count(role_text) == 1
    assert system.count(runtime_contract) == 1
    assert any(ord(character) > 127 for character in system)


async def _metered_client(tmp_path, provider):
    state = await world(tmp_path, "sqlite")
    ledger = RunRequestLedger(coordinator_for(state.tasks, state.paths))
    await ledger.bind(
        RUN, LLMRequestPolicy(mode="unlimited_requests", max_concurrent_requests=2),
    )
    loaded = load_specialist_profile(DEFAULT_SPECIALIST_PROFILE_PATH)
    return RunMeteredLLM(provider, ledger, prompts=loaded.prompts), loaded


@pytest.mark.parametrize(("stage", "role_id"), DEFAULT_STAGE_ROLES)
async def test_selected_default_korean_prompts_reach_provider_once(
    tmp_path, stage, role_id,
):
    provider = OfflineCapturingProvider()
    client, loaded = await _metered_client(tmp_path, provider)
    runtime_contract = f"Code-owned runtime contract [{stage}]"
    schema = RESPONSE_SCHEMA.copy()

    with llm_request_scope(RUN, f"owner-{stage}"), instruction_scope(stage, role_id):
        if stage == "native_action":
            await client.complete_native(
                messages=[{"role": "user", "content": "offline"}],
                tools=NATIVE_TOOLS,
                system=runtime_contract,
            )
        else:
            await client.complete_structured_with_usage(
                messages=[{"role": "user", "content": "offline"}],
                response_schema=schema,
                system=runtime_contract,
            )

    assert len(provider.calls) == 1
    method, request = provider.calls[0]
    assert method == ("native" if stage == "native_action" else "structured")
    if method == "native":
        assert request["tools"] == NATIVE_TOOLS
    else:
        assert request["response_schema"] == schema
    _assert_delivered_once(loaded, request, stage, role_id, runtime_contract)


async def test_parallel_prompt_scopes_do_not_leak_role_stage_or_runtime(tmp_path):
    provider = OfflineCapturingProvider()
    client, loaded = await _metered_client(tmp_path, provider)
    first_contract = "Code-owned runtime contract [native only]"
    second_contract = "Code-owned runtime contract [conversation only]"

    async def request(stage, role_id, contract, owner):
        with llm_request_scope(RUN, owner), instruction_scope(stage, role_id):
            await client.complete_structured_with_usage(
                messages=[{"role": "user", "content": owner}],
                response_schema=RESPONSE_SCHEMA,
                system=contract,
            )

    await asyncio.gather(
        request("native_action", "probe-worker", first_contract, "worker-owner"),
        request("conversation_decide", "conversation", second_contract, "chat-owner"),
    )

    assert len(provider.calls) == 2
    by_owner = {request["messages"][0]["content"]: request
                for _, request in provider.calls}
    native_request = by_owner["worker-owner"]
    conversation_request = by_owner["chat-owner"]
    _assert_delivered_once(
        loaded, native_request, "native_action", "probe-worker", first_contract,
    )
    _assert_delivered_once(
        loaded, conversation_request, "conversation_decide", "conversation", second_contract,
    )
    assert second_contract not in native_request["system"]
    assert first_contract not in conversation_request["system"]
    assert _role_documents(loaded, "conversation")[1] not in native_request["system"]
    assert _role_documents(loaded, "probe-worker")[1] not in conversation_request["system"]
    assert native_request["response_schema"] == RESPONSE_SCHEMA
    assert conversation_request["response_schema"] == RESPONSE_SCHEMA
