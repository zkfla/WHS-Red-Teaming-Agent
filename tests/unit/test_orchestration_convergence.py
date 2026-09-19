from redteam.attack_graph.service import AttackGraphService
from redteam.attack_graph.stores.memory import InMemoryAttackGraphStore
from redteam.core.contracts.graph import Hypothesis
from redteam.core.enums import AgentPhase, GraphNodeType
from redteam.core.ids import RunId
from redteam.orchestration.convergence import evaluate_convergence
from redteam.orchestration.state import TerminationReason
from redteam.orchestration.work.memory import InMemoryWorkStore


async def test_quiescent_clean_graph_converges_successfully() -> None:
    graph = AttackGraphService(InMemoryAttackGraphStore())
    decision = await evaluate_convergence(
        has_runnable_agent=False,
        graph=graph,
        work_store=InMemoryWorkStore(),
        run_id=RunId("run"),
    )

    assert decision.terminal is True
    assert decision.phase is AgentPhase.FINISHED
    assert decision.reason == TerminationReason.NO_ACTIONABLE_PLAN


async def test_open_hypothesis_prevents_false_success() -> None:
    graph = AttackGraphService(InMemoryAttackGraphStore())
    await graph.record_hypothesis(
        Hypothesis(
            hypothesis_id="hyp_open",
            description="still needs independent analysis",
            confidence=0.5,
            status="open",
        )
    )

    decision = await evaluate_convergence(
        has_runnable_agent=False,
        graph=graph,
        work_store=InMemoryWorkStore(),
        run_id=RunId("run"),
    )

    assert decision.phase is AgentPhase.FAILED
    assert decision.reason == TerminationReason.UNRESOLVED_HYPOTHESES
    assert decision.unresolved_hypothesis_count == 1


async def test_outstanding_cleanup_prevents_reporting_success() -> None:
    graph = AttackGraphService(InMemoryAttackGraphStore())
    host = await graph.upsert_node(GraphNodeType.HOST, key="created.local")
    await graph.register_created_resource(host.id, resource_type="test_account")

    decision = await evaluate_convergence(
        has_runnable_agent=False,
        graph=graph,
        work_store=InMemoryWorkStore(),
        run_id=RunId("run"),
    )

    assert decision.phase is AgentPhase.FAILED
    assert decision.reason == TerminationReason.CLEANUP_REQUIRED
    assert decision.outstanding_cleanup_count == 1
