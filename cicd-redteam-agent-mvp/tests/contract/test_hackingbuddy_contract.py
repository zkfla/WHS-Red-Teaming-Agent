import inspect

from hackingBuddyGPT.capability import Capability
from hackingBuddyGPT.usecases.agents import ChatAgent
from hackingBuddyGPT.utils.console import Console
from hackingBuddyGPT.utils.limits import Limits
from hackingBuddyGPT.utils.llm import LiteLLM
from hackingBuddyGPT.utils.logging import JsonlLogger

from cicd_redteam_agent.framework.capabilities import ListWorkflows
from cicd_redteam_agent.framework.cicd_agent import CICDChatAgent


def test_pinned_framework_contract() -> None:
    assert issubclass(CICDChatAgent, ChatAgent)
    assert issubclass(ListWorkflows, Capability)
    assert "max_rounds" in inspect.signature(Limits).parameters
    assert "max_tokens" in inspect.signature(Limits).parameters
    assert "max_cost" in inspect.signature(Limits).parameters
    assert "max_duration" in inspect.signature(Limits).parameters
    assert "context_size" in inspect.signature(LiteLLM).parameters
    assert "log_dir" in inspect.signature(JsonlLogger).parameters
    assert list(inspect.signature(ListWorkflows.__call__).parameters) == ["self"]
    assert callable(Console)
