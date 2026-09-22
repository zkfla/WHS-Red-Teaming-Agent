"""``WEB_API_AGENT`` playbook argument resolution: Graph context (an
ENDPOINT/SERVICE node) + the existing ``$target_ref``/``$path``
placeholder machinery (``PlanStep.bound_to``, extended, not replaced)
-> real, per-node ``target_ref``/``path`` arguments -- never a fixed
``"/"``, never a guess, and never an exception when the context a step
needs isn't there (section 16 A-E).
"""

from __future__ import annotations

import pytest

from redteam.attack_graph.identity import endpoint_node_id, service_node_id
from redteam.attack_graph.service import AttackGraphService
from redteam.attack_graph.stores.memory import InMemoryAttackGraphStore
from redteam.core.enums import AgentPhase, GraphNodeType
from redteam.orchestration.agents.builtin import WEB_API_AGENT
from redteam.orchestration.agents.definition import builtin_agent_definitions
from redteam.orchestration.agents.providers import PlaybookCandidateProvider

pytestmark = pytest.mark.asyncio

_ENDPOINT_STEP_ID = "inspect-http-endpoint"
_SERVICE_STEP_ID = "inspect-http-service"


async def _service_with_endpoints(*paths: str) -> AttackGraphService:
    service = AttackGraphService(InMemoryAttackGraphStore())
    await service.upsert_node(
        GraphNodeType.SERVICE, node_id=service_node_id("http", host="a.local")
    )
    for path in paths:
        await service.upsert_node(
            GraphNodeType.ENDPOINT, node_id=endpoint_node_id("GET", path, host="a.local")
        )
    return service


async def _propose(service: AttackGraphService, *, step: int = 1):  # noqa: ANN201
    observation = await service.observe(step=step)
    drafts = await PlaybookCandidateProvider().propose(
        WEB_API_AGENT, service.view(), observation, step=step
    )
    return drafts


def _by_step(drafts, step_id: str):  # noqa: ANN001, ANN201
    return [d for d in drafts if d.plan_step.step_id == step_id]


# -- A: activation (unaffected by this change, kept here for locality) -----


async def test_web_api_agent_activation_rule_is_satisfied_by_an_endpoint() -> None:
    service = await _service_with_endpoints("/")
    observation = await service.observe(step=1)
    definition = next(
        d for d in builtin_agent_definitions() if str(d.agent_id) == "web-api-agent"
    )
    assert definition.is_active(observation, AgentPhase.OBSERVE)


# -- B/C: target_ref and path resolve from real graph context --------------


async def test_target_ref_resolves_to_the_endpoints_host() -> None:
    service = await _service_with_endpoints("/")
    drafts = await _propose(service)

    endpoint_drafts = _by_step(drafts, _ENDPOINT_STEP_ID)
    assert len(endpoint_drafts) == 1
    assert endpoint_drafts[0].plan_step.arguments["target_ref"] == "a.local"


async def test_path_resolves_from_the_endpoint_node_not_hardcoded() -> None:
    service = await _service_with_endpoints("/login")
    drafts = await _propose(service)

    endpoint_drafts = _by_step(drafts, _ENDPOINT_STEP_ID)
    assert len(endpoint_drafts) == 1
    assert endpoint_drafts[0].plan_step.arguments["path"] == "/login"


# -- D: multiple endpoints each keep their own path -------------------------


async def test_multiple_endpoints_each_resolve_their_own_distinct_path() -> None:
    service = await _service_with_endpoints("/", "/login", "/api/account")
    drafts = await _propose(service)

    resolved_paths = {d.plan_step.arguments["path"] for d in _by_step(drafts, _ENDPOINT_STEP_ID)}
    assert resolved_paths == {"/", "/login", "/api/account"}


# -- E: missing context (SERVICE has no path) -> clean skip, no crash ------


async def test_service_only_context_produces_no_endpoint_style_candidate() -> None:
    service = AttackGraphService(InMemoryAttackGraphStore())
    await service.upsert_node(
        GraphNodeType.SERVICE, node_id=service_node_id("http", host="a.local")
    )

    drafts = await _propose(service)  # must not raise

    assert _by_step(drafts, _ENDPOINT_STEP_ID) == []
    assert _by_step(drafts, _SERVICE_STEP_ID) == []  # $path unresolvable against a ServiceTarget
