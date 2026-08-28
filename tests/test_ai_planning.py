import asyncio
from datetime import UTC, datetime
from pathlib import Path

import httpx

from whs_redteam.ai.models import PlannerDecision, PlanningContext
from whs_redteam.ai.provider import OpenAICompatiblePlanner
from whs_redteam.audit import AuditLogger
from whs_redteam.config import AIConfig, EngagementConfig, InjectionFixture
from whs_redteam.inventory import InventoryStore
from whs_redteam.models import (
    ActionResult,
    ActionStatus,
    AgentRunStatus,
    Capability,
    PlanItem,
)
from whs_redteam.models import (
    TestCategory as Category,
)
from whs_redteam.models import (
    TestPlan as Plan,
)
from whs_redteam.orchestration.orchestrator import AgentOrchestrator
from whs_redteam.planning.hybrid import HybridPlanner, planning_context


def _config(tmp_path: Path, *, provider: str = "none") -> EngagementConfig:
    return EngagementConfig(
        name="ai-planning-test",
        targets=("https://example.test",),
        inventory_db_path=tmp_path / "inventory.db",
        audit_log_path=tmp_path / "events.jsonl",
        injection_fixtures=(
            InjectionFixture(
                fixture_id="sql-candidate",
                approval_scope_url="https://example.test",
                target_url="https://example.test/items",
                parameter="id",
                baseline_value="1",
                parameter_kind="integer",
                checks=frozenset({"sqli"}),
            ),
        ),
        enabled_capabilities=frozenset({"sqli_safe", "llm_planning"}),
        approval_hmac_secret_env="TEST_APPROVAL_SECRET",
        planner_mode="hybrid",
        ai=AIConfig(provider=provider, model="test-model" if provider != "none" else ""),
    )


class _SelectCandidate:
    provider_name = "fake"
    model_name = "fake-model"

    async def plan(self, context: PlanningContext) -> PlannerDecision:
        return PlannerDecision(
            selected_actions=(
                {
                    "candidate_id": context.candidates[0].candidate_id,
                    "action": "run_sqli_safe",
                    "reason": "The fake model selected the only bounded candidate.",
                    "confidence": 0.9,
                },
            )
        )


class _SelectUnknown:
    provider_name = "fake"
    model_name = "fake-model"

    async def plan(self, context: PlanningContext) -> PlannerDecision:
        return PlannerDecision(
            selected_actions=(
                {
                    "candidate_id": "unknown-candidate",
                    "action": "run_sqli_safe",
                    "reason": "This must be rejected by the planner gate.",
                    "confidence": 0.9,
                },
            )
        )


def test_hybrid_planner_accepts_only_known_candidates(tmp_path: Path) -> None:
    config = _config(tmp_path)
    context = planning_context(
        config.injection_fixtures,
        remaining_requests=50,
        planning_round=0,
    )
    outcome = asyncio.run(
        HybridPlanner(config, AuditLogger(config.audit_log_path), provider=_SelectCandidate()).plan(
            context
        )
    )
    assert outcome.provider == "fake"
    assert outcome.fallback_used is False
    assert outcome.decision.selected_actions[0].candidate_id == "sql-candidate"


def test_hybrid_planner_falls_back_on_unknown_candidate(tmp_path: Path) -> None:
    config = _config(tmp_path)
    context = planning_context(
        config.injection_fixtures,
        remaining_requests=50,
        planning_round=0,
    )
    outcome = asyncio.run(
        HybridPlanner(config, AuditLogger(config.audit_log_path), provider=_SelectUnknown()).plan(
            context
        )
    )
    assert outcome.provider == "rules"
    assert outcome.fallback_used is True
    assert outcome.decision.selected_actions[0].candidate_id == "sql-candidate"


def test_hybrid_planner_records_unconfigured_provider_as_fallback(tmp_path: Path) -> None:
    config = _config(tmp_path, provider="none")
    context = planning_context(
        config.injection_fixtures,
        remaining_requests=50,
        planning_round=0,
    )
    outcome = asyncio.run(
        HybridPlanner(config, AuditLogger(config.audit_log_path)).plan(context)
    )
    assert outcome.provider == "rules"
    assert outcome.fallback_used is True
    assert outcome.fallback_reason == "ValueError"


def test_orchestrator_persists_hybrid_candidate_selection(tmp_path: Path) -> None:
    config = _config(tmp_path)
    logger = AuditLogger(config.audit_log_path)
    with InventoryStore(config.inventory_db_path) as inventory:
        orchestrator = AgentOrchestrator(
            config,
            inventory,
            logger,
            planner_provider=_SelectCandidate(),
        )
        plan = asyncio.run(orchestrator._plan_with_provider())
    assert plan.provider == "hybrid"
    assert plan.planner_provider == "fake"
    assert plan.selected_candidate_ids == ("sql-candidate",)
    assert plan.planner_fallback_used is False


def test_openai_compatible_planner_parses_structured_json_without_raw_values(
    tmp_path: Path,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"selected_actions": [], "finish": true, '
                                '"finish_reason": "done"}'
                            )
                        }
                    }
                ]
            },
        )

    config = _config(tmp_path, provider="openai_compatible")
    provider = OpenAICompatiblePlanner(
        config.ai,
        AuditLogger(config.audit_log_path),
        transport=httpx.MockTransport(handler),
    )
    context = planning_context(
        config.injection_fixtures,
        remaining_requests=50,
        planning_round=0,
    )
    asyncio.run(provider.plan(context))
    assert requests[0].url == "http://127.0.0.1:11434/v1/chat/completions"
    body = requests[0].content.decode()
    assert "sql-candidate" in body
    assert "baseline_value" not in body


def test_openai_compatible_planner_retries_transient_errors_and_records_usage(
    tmp_path: Path,
) -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(503)
        return httpx.Response(
            200,
            json={
                "usage": {"prompt_tokens": 12, "completion_tokens": 7},
                "choices": [
                    {
                        "message": {
                            "content": '{"selected_actions": [], "finish": true}'
                        }
                    }
                ],
            },
        )

    config = _config(tmp_path, provider="openai_compatible").model_copy(
        update={"ai": AIConfig(
            provider="openai_compatible",
            model="test-model",
            retry_attempts=1,
            retry_backoff_seconds=0,
        )}
    )
    provider = OpenAICompatiblePlanner(
        config.ai,
        AuditLogger(config.audit_log_path),
        transport=httpx.MockTransport(handler),
    )
    context = planning_context(
        config.injection_fixtures,
        remaining_requests=50,
        planning_round=0,
    )
    asyncio.run(provider.plan(context))
    assert attempts == 2
    assert provider.last_usage.api_calls == 2
    assert provider.last_usage.input_tokens == 12
    assert provider.last_usage.output_tokens == 7


def test_hybrid_planner_stops_when_api_call_budget_is_exhausted(tmp_path: Path) -> None:
    config = _config(tmp_path, provider="openai_compatible").model_copy(
        update={"ai": AIConfig(
            provider="openai_compatible",
            model="test-model",
            max_api_calls_per_run=1,
        )}
    )
    context = planning_context(
        config.injection_fixtures,
        remaining_requests=50,
        planning_round=1,
    )
    outcome = asyncio.run(
        HybridPlanner(
            config,
            AuditLogger(config.audit_log_path),
            provider=_SelectCandidate(),
        ).plan(context, api_calls_used=1)
    )
    assert outcome.fallback_used is True
    assert outcome.fallback_reason == "api_call_budget_exhausted"


def test_hybrid_planner_hard_caps_provider_retries_to_remaining_budget(
    tmp_path: Path,
) -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(503)

    config = _config(tmp_path, provider="openai_compatible").model_copy(
        update={"ai": AIConfig(
            provider="openai_compatible",
            model="test-model",
            max_api_calls_per_run=1,
            retry_attempts=4,
            retry_backoff_seconds=0,
        )}
    )
    provider = OpenAICompatiblePlanner(
        config.ai,
        AuditLogger(config.audit_log_path),
        transport=httpx.MockTransport(handler),
    )
    context = planning_context(
        config.injection_fixtures,
        remaining_requests=50,
        planning_round=0,
    )
    outcome = asyncio.run(
        HybridPlanner(config, AuditLogger(config.audit_log_path), provider=provider).plan(
            context
        )
    )
    assert attempts == 1
    assert outcome.fallback_used is True
    assert outcome.usage.api_calls == 1


def test_limited_replanning_only_selects_remaining_candidates(tmp_path: Path) -> None:
    first = _config(tmp_path)
    second = InjectionFixture(
        fixture_id="sql-candidate-two",
        approval_scope_url="https://example.test",
        target_url="https://example.test/search",
        parameter="q",
        baseline_value="safe",
        checks=frozenset({"sqli"}),
    )
    config = first.model_copy(
        update={"injection_fixtures": (*first.injection_fixtures, second)}
    )
    logger = AuditLogger(config.audit_log_path)
    with InventoryStore(config.inventory_db_path) as inventory:
        orchestrator = AgentOrchestrator(
            config,
            inventory,
            logger,
            planner_provider=_SelectCandidate(),
        )
        created = orchestrator.create("replan-run")
        plan = Plan(
            provider="fake",
            items=(
                PlanItem(
                    target_url="https://example.test",
                    category=Category.SECURITY_HEADERS,
                    reason="test",
                ),
            ),
            selected_candidate_ids=("sql-candidate",),
            planner_round=0,
        )
        checkpoint = created.model_copy(
            update={
                "status": AgentRunStatus.EXECUTING,
                "exploration_run_id": "replan-explore",
                "plan_json": plan.model_dump_json(),
            }
        )
        inventory.save_agent_run(checkpoint)
        now = datetime.now(UTC)
        inventory.save_action_result(
            ActionResult(
                action_id="sqli-action-one",
                run_id=created.run_id,
                capability=Capability.SQLI_SAFE,
                target_url="https://example.test/items",
                status=ActionStatus.SUCCEEDED,
                output={"fixture_id": "sql-candidate", "confirmed": False},
                started_at=now,
                finished_at=now,
            )
        )
        replanned = asyncio.run(
            orchestrator._maybe_replan(checkpoint, "validation-replan")
        )
    assert replanned is not None
    assert replanned.planner_round == 1
    assert replanned.selected_candidate_ids == ("sql-candidate-two",)
