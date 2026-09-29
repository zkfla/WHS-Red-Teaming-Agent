"""RC#1: the model generates candidate drafts; the same guards still gate them."""

from datetime import datetime, timezone
from typing import Any

from redteam.attack_graph.identity import make_node_id
from redteam.attack_graph.observation import GraphObservation
from redteam.core.contracts.graph import GraphNode
from redteam.core.enums import GraphNodeType
from redteam.core.ids import AgentId, GraphNodeId
from redteam.orchestration.agents.base import AgentSpec
from redteam.orchestration.agents.llm_provider import LLMCandidateProvider
from redteam.orchestration.agents.providers import GraphCandidateContext

ENDPOINT_KEY = "mock-bank:8080 GET /api/accounts/alice"
ENDPOINT_ID = str(make_node_id(GraphNodeType.ENDPOINT, ENDPOINT_KEY))


class _FakeView:
    """Minimal AttackGraphView stand-in: returns endpoint nodes carrying the
    ``observed_target`` fact the provider must read (never the model's word)."""

    def __init__(self, observed_by_id: dict[str, str] | None = None) -> None:
        self._observed = observed_by_id or {}

    async def get_node(self, node_id: GraphNodeId) -> GraphNode | None:
        key = str(node_id)
        facts = {}
        if key in self._observed:
            facts["observed_target"] = self._observed[key]
        now = datetime.now(timezone.utc)
        return GraphNode(
            id=GraphNodeId(key),
            type=GraphNodeType.ENDPOINT,
            properties={"facts": facts},
            created_at=now,
            updated_at=now,
        )


class _ScriptedLLM:
    def __init__(self, actions: list[dict[str, Any]]) -> None:
        self._actions = actions
        self.last_prompt = ""

    async def complete_structured(self, **kwargs: Any) -> dict[str, Any]:
        self.last_prompt = kwargs["messages"][0]["content"]
        return {"actions": self._actions}


def _context() -> GraphCandidateContext:
    observation = GraphObservation(
        step=1,
        nodes_by_type={"endpoint": [ENDPOINT_KEY]},
        node_ids_by_type={"endpoint": [ENDPOINT_ID]},
    )
    return GraphCandidateContext(
        view=_FakeView(),
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


async def test_executable_target_uses_graph_observed_path_not_model_word() -> None:
    # A node id normalises identifier segments (/api/orders/1234 ->
    # /api/orders/{id}); the concrete path must come from the graph's
    # observed_target fact, never the template and never a model-asserted value.
    key = "mock-bank:8080 GET /api/orders/{id}"
    node_id = str(make_node_id(GraphNodeType.ENDPOINT, key))
    observation = GraphObservation(
        step=1,
        nodes_by_type={"endpoint": [key]},
        node_ids_by_type={"endpoint": [node_id]},
    )
    context = GraphCandidateContext(
        view=_FakeView({node_id: "/api/orders/1234"}),
        observation=observation,
        step=1,
        available_operations=(("web", "http_request"),),
    )
    llm = _ScriptedLLM(
        [
            {
                "capability": "web",
                "operation": "http_request",
                "target_node_id": node_id,
                # A model claim that would send the request at the template.
                "observed": "/api/orders/{id}",
            }
        ]
    )
    batch = await LLMCandidateProvider(llm).propose_graph(_spec(), context)
    assert len(batch.candidates) == 1
    assert batch.candidates[0].plan_step.target.path == "/api/orders/1234"


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
        view=_FakeView(),
        observation=observation,
        step=1,
        task_id="task_authz",
        task_objective="Compare access to the account object",
        task_target_node_ids=(GraphNodeId(ENDPOINT_ID),),
    )
    llm = _ScriptedLLM([
        {"capability": "web", "operation": "http_request", "target_node_id": other}
    ])

    batch = await LLMCandidateProvider(llm).propose_graph(_spec(), context)

    assert "Assigned Task: Compare access to the account object" in llm.last_prompt
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
        view=_FakeView(),
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
