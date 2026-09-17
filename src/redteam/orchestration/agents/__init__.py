"""Agents: specs, the frontier, scheduling, and planners. Owned by Agent Team.

```text
AgentSpec       an agent's identity + the playbook it may propose from
PlanStep        one proposable action, with preconditions and its test
available_steps the frontier — what the graph supports right now
AgentRegistry   which agent runs this turn
Planner         which single step to take (playbook or LLM-driven)
```

See ``docs/agent/orchestration.md``.
"""

from redteam.orchestration.agents.base import (
    MAX_CANDIDATES_PER_STEP,
    AgentSpec,
    HypothesisDraft,
    Planner,
    PlanningContext,
    PlanStep,
    available_steps,
)
from redteam.orchestration.agents.builtin import MOBILE_AGENT, WEB_API_AGENT
from redteam.orchestration.agents.definition import (
    ActivationRule,
    AgentDefinition,
    ContextProfile,
    builtin_agent_definitions,
)
from redteam.orchestration.agents.planners import (
    LLMPlanner,
    PlaybookPlanner,
    build_planner,
)
from redteam.orchestration.agents.providers import (
    CandidateProvider,
    CandidateProviderRegistry,
    DuplicateProviderError,
    PlaybookCandidateProvider,
    default_provider_registry,
)
from redteam.orchestration.agents.registry import (
    RECON_AGENT,
    RECON_AGENT_ID,
    AgentRegistry,
    DuplicateAgentError,
    default_registry,
)
from redteam.orchestration.agents.result import AgentResult, FollowupHint

__all__ = [
    "AgentSpec",
    "PlanStep",
    "HypothesisDraft",
    "PlanningContext",
    "Planner",
    "available_steps",
    "MAX_CANDIDATES_PER_STEP",
    "AgentRegistry",
    "DuplicateAgentError",
    "default_registry",
    "RECON_AGENT",
    "RECON_AGENT_ID",
    "PlaybookPlanner",
    "LLMPlanner",
    "build_planner",
    "ActivationRule",
    "AgentDefinition",
    "ContextProfile",
    "builtin_agent_definitions",
    "AgentResult",
    "FollowupHint",
    "WEB_API_AGENT",
    "MOBILE_AGENT",
    "CandidateProvider",
    "CandidateProviderRegistry",
    "DuplicateProviderError",
    "PlaybookCandidateProvider",
    "default_provider_registry",
]
