from __future__ import annotations

from dataclasses import dataclass, field

from cicd_redteam_agent.framework.capabilities import CapabilityContext
from cicd_redteam_agent.framework.cicd_agent import CICDChatAgent
from cicd_redteam_agent.framework.runtime_support import CheckpointWriter, EventJournal


@dataclass
class CICDUseCase:
    agent: CICDChatAgent
    context: CapabilityContext
    limits: object
    checkpoint_writer: CheckpointWriter | None = None
    event_journal: EventJournal | None = None
    warnings: list[str] = field(default_factory=list)
    _checkpoint_failed: bool = field(default=False, init=False)

    async def _status(self, message: str) -> None:
        logger = getattr(self.agent, "log", None)
        status_message = getattr(logger, "status_message", None)
        if status_message is not None:
            await status_message(message)

    def _checkpoint(self, reason: str) -> None:
        if self.checkpoint_writer is None or self._checkpoint_failed:
            return
        try:
            self.checkpoint_writer.write(
                rounds=self.limits.rounds,
                tokens=self.limits.tokens,
                cost=self.limits.cost,
                reason=reason,
                active_duration_seconds=(
                    self.limits.duration.total_seconds() if self.limits.duration else 0.0
                ),
            )
        except OSError as exc:
            self._checkpoint_failed = True
            self.warnings.append(
                f"Checkpoint disabled after {type(exc).__name__}: {exc}"
            )

    async def run(self) -> str:
        self.limits.start()
        await self.agent.before_run(self.limits)
        runtime_metrics = getattr(self.agent, "runtime_metrics", None)
        no_progress = (
            runtime_metrics.no_progress_rounds if runtime_metrics is not None else 0
        )
        known_resources = len(self.context.state.inspected_resources)
        known_call_signatures = len(self.context.state.repeated_calls)
        known_memory_revision = self.context.state.working_memory.revision
        while not self.limits.reached() and not self.context.submitted:
            tokens_remaining = getattr(self.limits, "tokens_remaining", None)
            last_round_tokens = int(getattr(self.agent, "last_round_tokens", 0))
            if callable(tokens_remaining) and last_round_tokens:
                remaining = tokens_remaining()
                reserve = last_round_tokens
                if remaining is not None and remaining < reserve:
                    self.warnings.append(
                        "Agent stopped before starting a model round that was likely to "
                        "exceed the remaining token budget."
                    )
                    self._checkpoint("insufficient_token_reserve")
                    return "insufficient_token_reserve"
            round_number = self.limits.rounds + 1
            self.agent.active_round = round_number
            if self.event_journal is not None:
                self.event_journal.append(
                    "ROUND_STARTED",
                    round=round_number,
                    payload={"working_memory_revision": known_memory_revision},
                )
            await self._status(
                f"Agent round {self.limits.rounds + 1}/"
                f"{getattr(self.limits, 'max_rounds', 'unknown')}; "
                f"tokens={self.limits.tokens}; cost={self.limits.cost:.6f}."
            )
            state_before_round = self.context.state.model_dump(mode="json", by_alias=True)
            submitted_before_round = self.context.submitted
            prompt_history = getattr(self.agent, "_prompt_history", None)
            prompt_history_before_round = (
                list(prompt_history) if prompt_history is not None else None
            )
            try:
                await self.agent.perform_round(self.limits)
            except Exception as exc:
                restored = type(self.context.state).model_validate(state_before_round)
                for field_name in type(self.context.state).model_fields:
                    setattr(
                        self.context.state,
                        field_name,
                        getattr(restored, field_name),
                    )
                self.context.submitted = submitted_before_round
                if prompt_history_before_round is not None:
                    self.agent._prompt_history[:] = prompt_history_before_round
                if self.event_journal is not None:
                    self.event_journal.append(
                        "ROUND_ABORTED",
                        round=round_number,
                        payload={"error_type": type(exc).__name__},
                    )
                self.warnings.append(f"Model round failed: {type(exc).__name__}: {exc}")
                self._checkpoint("model_error")
                return "model_error"
            if self.event_journal is not None:
                self.event_journal.append("ROUND_COMPLETED", round=self.limits.rounds)
            self._checkpoint("round_complete")
            await self._status(
                f"Completed round {self.limits.rounds}; "
                f"last_capability={getattr(self.agent, 'last_capability', None) or 'unknown'}; "
                f"tokens={self.limits.tokens}; cost={self.limits.cost:.6f}."
            )
            current_resources = len(self.context.state.inspected_resources)
            current_call_signatures = len(self.context.state.repeated_calls)
            current_memory_revision = self.context.state.working_memory.revision
            repeated = max(self.context.state.repeated_calls.values(), default=0)
            if (
                current_resources == known_resources
                and current_call_signatures == known_call_signatures
                and current_memory_revision == known_memory_revision
            ):
                no_progress += 1
            else:
                no_progress = 0
                known_resources = current_resources
                known_call_signatures = current_call_signatures
                known_memory_revision = current_memory_revision
            if runtime_metrics is not None:
                runtime_metrics.no_progress_rounds = no_progress
            if repeated >= 4 or no_progress >= 4:
                self.warnings.append("Agent stopped after repeated calls or no progress.")
                self._checkpoint("no_progress")
                return "no_progress"
        if self.context.submitted:
            self.limits.complete()
            if runtime_metrics is not None:
                runtime_metrics.no_progress_rounds = no_progress
            self._checkpoint("proposal_validated")
            return "proposal_validated"
        self._checkpoint(self.limits.reason or "limit_reached")
        return self.limits.reason or "limit_reached"
