import json
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from hackingBuddyGPT.utils import LLMResult
from hackingBuddyGPT.utils.limits import Limits

from cicd_redteam_agent.application.state import create_initial_state
from cicd_redteam_agent.domain.github_actions_parser import discover_workflows, parse_workflow
from cicd_redteam_agent.framework.capabilities import CapabilityContext, build_capabilities
from cicd_redteam_agent.framework.cicd_agent import CICDChatAgent
from cicd_redteam_agent.framework.cicd_usecase import CICDUseCase
from cicd_redteam_agent.security.repository_boundary import RepositoryBoundary

FIXTURES = Path(__file__).parents[1] / "fixtures"


class FakeLogger:
    async def system_message(self, _message):
        return 0

    async def limit_message(self, _message):
        return 0

    async def call_response(self, _result):
        return 0

    async def add_tool_call(self, *_args):
        return 0

    async def status_message(self, _message):
        return 0


class FakeLLM:
    def __init__(self):
        self.round = 0

    def get_response(self, _prompt, *, capabilities):
        calls = [
            ("ListWorkflows", {}),
            (
                "InspectRepositoryFile",
                {"path": "scripts/deploy.sh", "start_line": 1, "end_line": 2},
            ),
            ("SubmitAnalysis", {"proposal_json": json.dumps({"schema_version": "1.0"})}),
        ]
        name, arguments = calls[self.round]
        self.round += 1
        tool_call = SimpleNamespace(
            id=f"call-{self.round}",
            function=SimpleNamespace(name=name, arguments=json.dumps(arguments)),
        )
        message = SimpleNamespace(tool_calls=[tool_call], content="")
        return LLMResult(message, "", "", "", timedelta(), 1, 1, 0, "", 0.0)


@pytest.mark.asyncio
async def test_fake_llm_list_inspect_submit_flow() -> None:
    boundary = RepositoryBoundary(FIXTURES / "oidc-risk")
    state = create_initial_state(boundary)
    parsed = parse_workflow(boundary, discover_workflows(boundary)[0])
    state.workflows = [parsed.workflow]
    state.evidence.update(parsed.evidence)
    context = CapabilityContext(state, boundary)
    agent = CICDChatAgent(log=FakeLogger(), llm=FakeLLM(), prompt="trusted system prompt")
    agent._capabilities = build_capabilities(context)
    limits = Limits(max_rounds=5, max_tokens=100, max_cost=1, max_duration=30)
    reason = await CICDUseCase(agent, context, limits).run()
    assert reason == "proposal_validated"
    assert context.submitted
    assert "scripts/deploy.sh" in state.inspected_resources
    assert limits.rounds == 3
