"""RC#1: the model generates candidate drafts; the same guards still gate them."""

from typing import Any

from redteam.attack_graph.identity import make_node_id
from redteam.attack_graph.observation import GraphObservation
from redteam.core.enums import GraphNodeType
from redteam.core.ids import AgentId, GraphNodeId
from redteam.orchestration.agents.base import AgentSpec
from redteam.orchestration.agents.llm_provider import LLMCandidateProvider
from redteam.orchestration.agents.providers import GraphCandidateContext

ENDPOINT_KEY = "mock-bank:8080 GET /api/accounts/alice"
ENDPOINT_ID = str(make_node_id(GraphNodeType.ENDPOINT, ENDPOINT_KEY))


class _ScriptedLLM:
    def __init__(self, actions: list[dict[str, Any]]) -> None:
        self._actions = actions
        self.last_prompt = ""
        self.last_system = ""

    async def complete_structured(self, **kwargs: Any) -> dict[str, Any]:
        self.last_prompt = kwargs["messages"][0]["content"]
        self.last_system = kwargs["system"]
        return {"actions": self._actions}


def _context() -> GraphCandidateContext:
    observation = GraphObservation(
        step=1,
        nodes_by_type={"endpoint": [ENDPOINT_KEY]},
        node_ids_by_type={"endpoint": [ENDPOINT_ID]},
    )
    return GraphCandidateContext(
        view=None,  # type: ignore[arg-type]  # unused by this provider
        observation=observation,
        step=1,
        available_operations=(("web", "http_request"),),
    )


def _spec() -> AgentSpec:
    return AgentSpec(agent_id=AgentId("web-api"), name="web-api", description="web/api")


async def test_model_generates_a_novel_action_not_in_any_playbook() -> None:
    llm = _ScriptedLLM(
        [
            {
                "capability": "web",
                "operation": "http_request",
                "target_node_id": ENDPOINT_ID,
                "arguments": {"path": "/api/accounts/bob"},
                "rationale": "sibling account id -> test for BOLA",
            }
        ]
    )
    batch = await LLMCandidateProvider(llm).propose_graph(_spec(), _context())
    assert len(batch.candidates) == 1
    step = batch.candidates[0].plan_step
    assert step.capability == "web"
    assert step.operation == "http_request"
    assert step.arguments == {"path": "/api/accounts/bob"}
    assert "MUST be natural Korean" in llm.last_system


async def test_explicit_live_tool_id_is_a_hard_compatibility_preference() -> None:
    llm = _ScriptedLLM(
        [
            {
                "capability": "web",
                "operation": "http_request",
                "target_node_id": ENDPOINT_ID,
                "tool_id": "web.http_request",
            }
        ]
    )
    batch = await LLMCandidateProvider(llm).propose_graph(_spec(), _context())
    step = batch.candidates[0].plan_step
    assert step.compatible_tool_ids == ["web.http_request"]


async def test_model_target_ref_is_rebound_to_the_known_graph_target() -> None:
    context = _context()
    context = GraphCandidateContext(
        **{
            **context.__dict__,
            "available_tools": (
                {
                    "tool_id": "web.http_request",
                    "required_arguments": ("target_ref",),
                },
            ),
        }
    )
    llm = _ScriptedLLM(
        [
            {
                "capability": "web",
                "operation": "http_request",
                "target_node_id": ENDPOINT_ID,
                "tool_id": "web.http_request",
                "arguments": {"target_ref": "endpoint:invented"},
            }
        ]
    )
    batch = await LLMCandidateProvider(llm).propose_graph(_spec(), context)
    assert batch.candidates[0].plan_step.arguments["target_ref"] == "mock-bank"


async def test_hallucinated_target_is_dropped_not_executed() -> None:
    llm = _ScriptedLLM(
        [
            {
                "capability": "web",
                "operation": "http_request",
                "target_node_id": "endpoint:does-not-exist",
                "arguments": {},
                "rationale": "made-up target",
            }
        ]
    )
    batch = await LLMCandidateProvider(llm).propose_graph(_spec(), _context())
    assert batch.candidates == []
    assert [drop.reason for drop in batch.drops] == ["unknown_target_node"]


async def test_generator_receives_and_obeys_the_assigned_task() -> None:
    other = str(make_node_id(GraphNodeType.ENDPOINT, "mock-bank GET /unrelated"))
    observation = GraphObservation(
        step=1,
        nodes_by_type={"endpoint": [ENDPOINT_KEY, "mock-bank GET /unrelated"]},
        node_ids_by_type={"endpoint": [ENDPOINT_ID, other]},
    )
    context = GraphCandidateContext(
        view=None,  # type: ignore[arg-type]
        observation=observation,
        step=1,
        task_id="task_authz",
        task_objective="Compare access to the account object",
        mission_objective="Assess only the operator-requested account flow",
        task_target_node_ids=(GraphNodeId(ENDPOINT_ID),),
    )
    llm = _ScriptedLLM([
        {"capability": "web", "operation": "http_request", "target_node_id": other}
    ])

    batch = await LLMCandidateProvider(llm).propose_graph(_spec(), context)

    assert "Assigned Task: Compare access to the account object" in llm.last_prompt
    assert "Operator Mission: Assess only the operator-requested account flow" in llm.last_prompt
    assert f"  - {other}" not in llm.last_prompt
    assert batch.candidates == []
    assert [drop.reason for drop in batch.drops] == ["unknown_target_node"]


async def test_raw_outcome_is_fed_to_the_model_prompt() -> None:
    # RC#2: the model must see the raw result (status/error), not just the
    # graph summary, so it can propose a follow-up past the playbook.
    observation = GraphObservation(
        step=1,
        nodes_by_type={"endpoint": [ENDPOINT_KEY]},
        node_ids_by_type={"endpoint": [ENDPOINT_ID]},
    )
    context = GraphCandidateContext(
        view=None,  # type: ignore[arg-type]
        observation=observation,
        step=1,
        available_operations=(("web", "http_request"),),
        evidence_excerpts=(
            {
                "target_node_id": ENDPOINT_ID,
                "status_code": 401,
                "error_code": "tool_output_schema_mismatch",
                "summary": "probe",
            },
        ),
    )
    llm = _ScriptedLLM([])
    await LLMCandidateProvider(llm).propose_graph(_spec(), context)
    assert "status=401" in llm.last_prompt
    assert "error=tool_output_schema_mismatch" in llm.last_prompt


async def test_generation_failure_degrades_to_empty_not_crash() -> None:
    class _Boom:
        async def complete_structured(self, **_: Any) -> dict[str, Any]:
            raise RuntimeError("provider exploded")

    batch = await LLMCandidateProvider(_Boom()).propose_graph(_spec(), _context())
    assert batch.candidates == []
    assert batch.drops == []


async def test_one_turn_carries_public_response_summary_and_assumptions() -> None:
    llm = _ScriptedLLM([])

    async def complete_structured(**kwargs: Any) -> dict[str, Any]:
        llm.last_prompt = kwargs["messages"][0]["content"]
        return {
            "intent": "finish",
            "response": "현재 관찰로 필요한 확인을 마쳤습니다.",
            "decision_summary": "추가 실행보다 결과 정리가 적절합니다.",
            "assumptions": ["등록된 대상만 평가했습니다."],
            "actions": [],
        }

    llm.complete_structured = complete_structured  # type: ignore[method-assign]
    provider = LLMCandidateProvider(llm)
    batch = await provider.propose_graph(_spec(), _context())

    assert batch.candidates == []
    assert provider.last_decision is not None
    assert provider.last_decision.intent == "finish"
    assert provider.last_decision.response.startswith("현재 관찰")
    assert provider.last_decision.assumptions == ["등록된 대상만 평가했습니다."]
