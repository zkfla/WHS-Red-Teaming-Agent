import json
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from hackingBuddyGPT.utils import LLMResult
from hackingBuddyGPT.utils.limits import Limits

from cicd_redteam_agent.application.objective import compile_objective_goal
from cicd_redteam_agent.application.state import create_initial_state
from cicd_redteam_agent.domain.github_actions_parser import (
    discover_workflows,
    parse_workflow,
    stable_id,
)
from cicd_redteam_agent.framework.capabilities import CapabilityContext, build_capabilities
from cicd_redteam_agent.framework.cicd_agent import CICDChatAgent
from cicd_redteam_agent.framework.cicd_usecase import CICDUseCase
from cicd_redteam_agent.framework.runtime_support import CheckpointWriter, RetryPolicy
from cicd_redteam_agent.security.repository_boundary import RepositoryBoundary

FIXTURES = Path(__file__).parents[1] / "fixtures"


class FakeLogger:
    def __init__(self) -> None:
        self.logged_results = []

    async def system_message(self, _message):
        return 0

    async def limit_message(self, _message):
        return 0

    async def call_response(self, result):
        self.logged_results.append(result)
        return 0

    async def add_tool_call(self, *_args):
        return 0

    async def status_message(self, _message):
        return 0


class FakeLLM:
    def __init__(self, proposal: dict[str, object]):
        self.round = 0
        self.proposal = proposal
        self.prompts = []

    def get_response(self, prompt, *, capabilities):
        self.prompts.append(prompt)
        calls = [
            ("ListWorkflows", {}),
            (
                "InspectRepositoryFile",
                {
                    "path": "scripts/deploy.sh",
                    "start_line": 1,
                    "end_line": 2,
                    "working_memory_patch_json": json.dumps(
                        {
                            "schema_version": "1.0",
                            "expected_revision": 0,
                            "upserts": [
                                {
                                    "schema_version": "1.0",
                                    "claim": "The deploy script may expose a credential path",
                                    "status": "ACTIVE",
                                    "decision_summary": (
                                        "The workflow points to a repository-controlled script."
                                    ),
                                    "next_check": {
                                        "schema_version": "1.0",
                                        "objective": "Inspect the deploy script",
                                        "preferred_capability": "InspectRepositoryFile",
                                        "target": "scripts/deploy.sh",
                                    },
                                }
                            ],
                        }
                    ),
                },
            ),
            ("SubmitAnalysis", {"proposal_json": json.dumps(self.proposal)}),
        ]
        name, arguments = calls[self.round]
        self.round += 1
        tool_call = SimpleNamespace(
            id=f"call-{self.round}",
            function=SimpleNamespace(name=name, arguments=json.dumps(arguments)),
        )
        message = SimpleNamespace(tool_calls=[tool_call], content="")
        return LLMResult(
            message,
            str(prompt),
            "",
            "",
            timedelta(),
            1,
            1,
            0,
            "",
            0.0,
        )


class TransientFakeLLM:
    def __init__(self) -> None:
        self.attempts = 0

    def get_response(self, _prompt, *, capabilities):
        self.attempts += 1
        if self.attempts < 3:
            raise TimeoutError("temporary model timeout")
        tool_call = SimpleNamespace(
            id="call-after-retry",
            function=SimpleNamespace(name="ListWorkflows", arguments="{}"),
        )
        message = SimpleNamespace(tool_calls=[tool_call], content="")
        return LLMResult(message, "", "", "", timedelta(), 1, 1, 0, "", 0.0)


@pytest.mark.asyncio
async def test_fake_llm_list_inspect_submit_flow(tmp_path: Path) -> None:
    boundary = RepositoryBoundary(FIXTURES / "oidc-risk")
    state = create_initial_state(boundary)
    parsed = parse_workflow(boundary, discover_workflows(boundary)[0])
    state.workflows = [parsed.workflow]
    state.evidence.update(parsed.evidence)
    context = CapabilityContext(state, boundary)
    excerpt = boundary.read_slice("scripts/deploy.sh", 1, 2)
    evidence_id = stable_id("ev", "scripts/deploy.sh", 1, 2, excerpt)
    proposal = {
        "schema_version": "1.0",
        "findings": [
            {
                "schema_version": "1.0",
                "finding_id": "finding_fake_llm_candidate",
                "rule_id": "FAKE-LLM-CANDIDATE",
                "title": "Fake LLM candidate",
                "status": "CANDIDATE",
                "evidence_ids": [evidence_id],
                "missing_evidence": ["runtime behavior"],
                "confidence": "LOW",
                "summary": "The fake model submits a bounded evidence-backed candidate.",
            }
        ],
    }
    llm = FakeLLM(proposal)
    logger = FakeLogger()
    agent = CICDChatAgent(
        log=logger,
        llm=llm,
        prompt="trusted system prompt",
        state=state,
        objective="inspect the credential path only",
        objective_goal=compile_objective_goal(
            "inspect the credential path only"
        ).as_dict(),
    )
    agent._capabilities = build_capabilities(context)
    limits = Limits(max_rounds=5, max_tokens=100, max_cost=1, max_duration=30)
    checkpoint_path = tmp_path / "checkpoint.json"
    checkpoint = CheckpointWriter(
        checkpoint_path,
        state,
        "fake",
        "fake-model",
        agent.runtime_metrics,
    )
    reason = await CICDUseCase(agent, context, limits, checkpoint).run()
    assert reason == "proposal_validated"
    assert context.submitted
    assert "scripts/deploy.sh" in state.inspected_resources
    assert state.findings[0].finding_id == "finding_fake_llm_candidate"
    assert state.findings[0].evidence_ids == [evidence_id]
    assert state.working_memory.revision == 1
    assert state.working_memory.hypotheses[0].status.value == "ACTIVE"
    for model_prompt in llm.prompts:
        assert model_prompt[-2]["name"] == "analysis_objective"
        assert "inspect the credential path only" in model_prompt[-2]["content"]
        assert "STRUCTURED_GOAL_HINTS" in model_prompt[-2]["content"]
        assert model_prompt[-1]["name"] == "agent_state_projection"
    assert all(
        getattr(message, "name", None) != "analysis_objective"
        for message in agent._prompt_history
    )
    assert logger.logged_results
    assert all(
        "inspect the credential path only" not in result.prompt
        for result in logger.logged_results
    )
    assert all("[OBJECTIVE_REDACTED]" in result.prompt for result in logger.logged_results)
    final_projection = llm.prompts[-1][-1]
    assert final_projection["name"] == "agent_state_projection"
    assert '"revision": 1' in final_projection["content"]
    assert limits.rounds == 3
    assert agent.runtime_metrics.model_attempts == 3
    assert agent.runtime_metrics.checkpoints_written == 4
    assert json.loads(checkpoint_path.read_text(encoding="utf-8"))["reason"] == (
        "proposal_validated"
    )


@pytest.mark.asyncio
async def test_agent_retries_one_model_turn_without_duplicating_history() -> None:
    boundary = RepositoryBoundary(FIXTURES / "oidc-risk")
    state = create_initial_state(boundary)
    context = CapabilityContext(state, boundary)
    llm = TransientFakeLLM()
    agent = CICDChatAgent(
        log=FakeLogger(),
        llm=llm,
        prompt="trusted system prompt",
        state=state,
        retry_policy=RetryPolicy(max_attempts=3, initial_delay=0, max_delay=0),
    )
    agent._capabilities = build_capabilities(context)
    limits = Limits(max_rounds=1, max_tokens=100, max_cost=1, max_duration=30)

    reason = await CICDUseCase(agent, context, limits).run()

    assert reason == "Reached maximum rounds (1)"
    assert llm.attempts == 3
    assert agent.runtime_metrics.retries == 2
    assert len(agent._prompt_history) == 4
