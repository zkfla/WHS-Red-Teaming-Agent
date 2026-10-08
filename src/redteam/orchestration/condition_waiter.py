"""Injected, bounded wake-up seam; the loop does not know condition/tool names."""

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from redteam.orchestration.context import RuntimeDeps
    from redteam.orchestration.state import RuntimeState


class ConditionWaiter(Protocol):
    async def wait_for_change(self, runtime: "RuntimeState", deps: "RuntimeDeps") -> bool:
        """Wake OBSERVE only on a related change; never reset lifetime budgets.

        Providers own deadlines, stop/cancellation, durable dedup, and finite
        total wait limits. No tool execution or inference belongs in this wait.
        """
        ...
