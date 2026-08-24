from pathlib import Path

import pytest

from cicd_redteam_agent.application.state import create_initial_state
from cicd_redteam_agent.framework.capabilities import CapabilityContext
from cicd_redteam_agent.framework.cicd_usecase import CICDUseCase
from cicd_redteam_agent.security.repository_boundary import RepositoryBoundary


class StubLimits:
    def __init__(self, *, reached: bool = False, reason: str | None = None) -> None:
        self._reached = reached
        self.reason = reason
        self.rounds = 0
        self.tokens = 0
        self.cost = 0.0
        self.started = False
        self.completed = False

    def start(self) -> None:
        self.started = True

    def reached(self) -> bool:
        return self._reached

    def complete(self) -> None:
        self.completed = True


class FailingAgent:
    async def before_run(self, _limits: StubLimits) -> None:
        return None

    async def perform_round(self, _limits: StubLimits) -> None:
        raise RuntimeError("rate limit")


class RepeatingAgent:
    def __init__(self, context: CapabilityContext) -> None:
        self.context = context

    async def before_run(self, _limits: StubLimits) -> None:
        return None

    async def perform_round(self, limits: StubLimits) -> None:
        self.context.record_call("same-call", {})
        limits.rounds += 1


class VaryingAgent:
    def __init__(self, context: CapabilityContext) -> None:
        self.context = context
        self.round = 0

    async def before_run(self, _limits: StubLimits) -> None:
        return None

    async def perform_round(self, limits: StubLimits) -> None:
        self.round += 1
        self.context.record_call("different-call", {"round": self.round})
        limits.rounds += 1
        if self.round == 5:
            self.context.submitted = True


def _context(tmp_path: Path) -> CapabilityContext:
    boundary = RepositoryBoundary(tmp_path)
    state = create_initial_state(boundary)
    state.pending_questions = ["preserve deterministic state"]
    return CapabilityContext(state, boundary)


@pytest.mark.asyncio
async def test_model_error_preserves_existing_state(tmp_path: Path) -> None:
    context = _context(tmp_path)
    usecase = CICDUseCase(FailingAgent(), context, StubLimits())

    reason = await usecase.run()

    assert reason == "model_error"
    assert context.state.pending_questions == ["preserve deterministic state"]
    assert "RuntimeError: rate limit" in usecase.warnings[0]


@pytest.mark.asyncio
async def test_repeated_calls_stop_after_four_rounds(tmp_path: Path) -> None:
    context = _context(tmp_path)
    limits = StubLimits()

    reason = await CICDUseCase(RepeatingAgent(context), context, limits).run()

    assert reason == "no_progress"
    assert limits.rounds == 4


@pytest.mark.asyncio
async def test_unique_calls_count_as_progress_until_submission(tmp_path: Path) -> None:
    context = _context(tmp_path)
    limits = StubLimits()

    reason = await CICDUseCase(VaryingAgent(context), context, limits).run()

    assert reason == "proposal_validated"
    assert limits.rounds == 5
    assert max(context.state.repeated_calls.values()) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("limit_reason", ["max_rounds", "max_tokens", "max_cost", "max_duration"])
async def test_limit_reason_is_returned_without_losing_state(
    tmp_path: Path, limit_reason: str
) -> None:
    context = _context(tmp_path)
    limits = StubLimits(reached=True, reason=limit_reason)

    reason = await CICDUseCase(RepeatingAgent(context), context, limits).run()

    assert reason == limit_reason
    assert context.state.pending_questions == ["preserve deterministic state"]
