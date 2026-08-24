from __future__ import annotations

from dataclasses import dataclass, field

from cicd_redteam_agent.framework.capabilities import CapabilityContext
from cicd_redteam_agent.framework.cicd_agent import CICDChatAgent


@dataclass
class CICDUseCase:
    agent: CICDChatAgent
    context: CapabilityContext
    limits: object
    warnings: list[str] = field(default_factory=list)

    async def run(self) -> str:
        self.limits.start()
        await self.agent.before_run(self.limits)
        no_progress = 0
        known_resources = len(self.context.state.inspected_resources)
        known_call_signatures = len(self.context.state.repeated_calls)
        while not self.limits.reached() and not self.context.submitted:
            try:
                await self.agent.perform_round(self.limits)
            except Exception as exc:
                self.warnings.append(f"Model round failed: {type(exc).__name__}: {exc}")
                return "model_error"
            current_resources = len(self.context.state.inspected_resources)
            current_call_signatures = len(self.context.state.repeated_calls)
            repeated = max(self.context.state.repeated_calls.values(), default=0)
            if (
                current_resources == known_resources
                and current_call_signatures == known_call_signatures
            ):
                no_progress += 1
            else:
                no_progress = 0
                known_resources = current_resources
                known_call_signatures = current_call_signatures
            if repeated >= 4 or no_progress >= 4:
                self.warnings.append("Agent stopped after repeated calls or no progress.")
                return "no_progress"
        if self.context.submitted:
            self.limits.complete()
            return "proposal_validated"
        return self.limits.reason or "limit_reached"
