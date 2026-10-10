"""Public run/CLI entry points, scripted provider only; no external requests."""

import pytest

from redteam.app.bootstrap import run_engagement
from redteam.app.cli.commands.run import OperatorConsole, RuntimeIdentity
from redteam.app.cli.conversation import ConversationAssistant, ConversationError
from redteam.app.cli.input_router import InputRouter
from redteam.core.contracts.llm_policy import LLMRequestPolicy
from redteam.core.ports.llm import NativeCompletion
from redteam.orchestration.coordination import coordinator_for
from redteam.orchestration.llm_requests import RunRequestLedger
from redteam.orchestration.runner import AgentLoopRunner
from redteam.orchestration.settings import OrchestrationSettings
from tests.integration.test_native_react_loop import ScriptedNativeLLM, _engagement, _native_deps
from tests.unit.test_orchestration_coordination import MISSION, RUN, world


async def test_public_run_dispatch_and_report_use_shared_ledger(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    provider = ScriptedNativeLLM([NativeCompletion(tool_calls=(), text="not enough evidence")])
    engagement = _engagement()
    deps = _native_deps(engagement, provider, settings=OrchestrationSettings(
        max_steps=3, checkpointing=False,
        llm_request_policy=LLMRequestPolicy(mode="bounded", max_requests=1),
    ))
    final = await run_engagement(engagement, deps=deps)
    ledger = RunRequestLedger(coordinator_for(deps.task_graph_store, deps.path_store))
    usage = await ledger.snapshot(str(final.run_id))
    assert len(provider.native_turns) == usage["spent"] == 1
    assert usage["reserved"] == 0
    assert final.llm_request_policy["max_requests"] == 1
    # No invented usage, and no false goal success on a plain model answer.
    assert usage["unknown_usage"] == 1
    assert final.termination_reason != "goal_achieved"


async def test_runner_refuses_changed_or_removed_policy_on_resume(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from dataclasses import replace

    provider = ScriptedNativeLLM([])
    engagement = _engagement()
    deps = _native_deps(engagement, provider, settings=OrchestrationSettings(
        max_steps=2, checkpointing=True,
        llm_request_policy=LLMRequestPolicy(mode="bounded", max_requests=2),
    ))
    final = await run_engagement(engagement, deps=deps)
    for selected in (None, LLMRequestPolicy(mode="unlimited_requests")):
        changed = replace(deps, settings=deps.settings.model_copy(update={
            "llm_request_policy": selected,
        }))
        assert not await AgentLoopRunner(changed)._binding_for(final)


class ChatProvider:
    def __init__(self):
        self.calls = 0

    async def complete_structured(self, **kwargs):
        self.calls += 1
        assert kwargs["num_retries"] == 0
        return {"mode": "answer", "response": "Recorded observations are not findings."}


async def test_live_conversation_uses_same_run_budget_not_unmetered_assistant(tmp_path):
    w = await world(tmp_path, "memory")
    provider = ChatProvider()
    engagement = _engagement()
    deps = _native_deps(engagement, provider, settings=OrchestrationSettings(
        llm_request_policy=LLMRequestPolicy(mode="bounded", max_requests=1),
    ))
    from dataclasses import replace

    from redteam.orchestration.metered_llm import RunMeteredLLM

    coordinator = coordinator_for(w.tasks, w.paths)
    ledger = RunRequestLedger(coordinator)
    await ledger.bind(RUN, deps.settings.llm_request_policy)
    deps = replace(deps, task_graph_store=w.tasks, path_store=w.paths,
                   coordinator=coordinator, llm_client=RunMeteredLLM(provider, ledger))
    unbound = ChatProvider()
    router = InputRouter(ConversationAssistant(unbound))
    console = OperatorConsole(deps, lambda _: None, identity=RuntimeIdentity(
        run_id=RUN, mission_id=MISSION, engagement_id=engagement.engagement_id,
    ))
    kwargs = dict(context="Run context", active=True, engagement_name="unit", scope=[])
    assert (await console.route_conversation(router, "status?", **kwargs)).mode == "answer"
    with pytest.raises(ConversationError):
        await console.route_conversation(router, "more?", **kwargs)
    assert provider.calls == 1
    assert unbound.calls == 0
    assert (await ledger.snapshot(RUN))["spent"] == 1
