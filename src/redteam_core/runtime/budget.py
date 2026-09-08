from __future__ import annotations

from redteam_core.models.runtime import RuntimeState, RuntimeUsage


def record_runtime_usage(state: RuntimeState, usage: RuntimeUsage) -> RuntimeState:
    if not usage.dominates(state.usage):
        raise ValueError("runtime usage counters cannot move backwards")
    if usage == state.usage:
        return state
    candidate = state.model_copy(deep=True)
    candidate.usage = usage
    candidate.revision += 1
    return RuntimeState.model_validate(candidate.model_dump(mode="python"))
