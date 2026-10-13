"""One request facade shared by component factories and independent instances."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from redteam.core.ports.agent_prompts import AgentPrompts
from redteam.core.ports.llm import LLMProviderFailure, StructuredCompletion
from redteam.core.request_scope import RequestScope, current_request_scope, llm_request_scope
from redteam.orchestration.llm_requests import RequestAdmissionError, RunRequestLedger

__all__ = ["RequestScope", "current_request_scope", "llm_request_scope", "RunMeteredLLM",
           "configure_request_metering", "llm_request_guard", "current_request_guard"]


_guard: ContextVar[Callable[[], Awaitable[None]] | None] = ContextVar(
    "llm_request_guard", default=None,
)


@contextmanager
def llm_request_guard(
    guard: Callable[[], Awaitable[None]] | None,
) -> Iterator[None]:
    """Per-task time/mission re-check run AFTER semaphore/cooldown wait.

    The planner/engine set this around their model requests; the metered
    facade invokes it immediately BEFORE reserve/dispatch so exhaustion
    while waiting for a slot (or between strategy and atomization) still
    stops with a structured budget error instead of an empty success.
    Context-local, so concurrent Runs never share a guard.
    """
    token = _guard.set(guard)
    try:
        yield
    finally:
        _guard.reset(token)


def current_request_guard() -> Callable[[], Awaitable[None]] | None:
    return _guard.get()


class RunMeteredLLM:
    """SDK retries are disabled; each explicit retry reserves a new request.

    Used only for configured request policies. Missing Run scope fails closed,
    including component calls accidentally made outside the run lifecycle.
    The adapter must honor num_retries=0 and issue one provider request per call.
    """

    def __init__(
        self, client: Any, ledger: RunRequestLedger, *, sleep=asyncio.sleep,
        prompts: AgentPrompts | None = None,
        request_semaphore: asyncio.Semaphore | None = None,
    ) -> None:
        self.client = client
        self.ledger = ledger
        self.sleep = sleep
        self.prompts = prompts
        self.request_semaphore = request_semaphore

    async def _call(self, method: str, **kwargs: Any) -> Any:
        if self.request_semaphore is None:
            return await self._call_admitted(method, **kwargs)
        async with self.request_semaphore:
            return await self._call_admitted(method, **kwargs)

    async def _call_admitted(self, method: str, **kwargs: Any) -> Any:
        scope = current_request_scope()
        if scope is None:
            raise RequestAdmissionError("llm_request_run_scope_missing")
        call = getattr(self.client, method, None)
        if call is None:
            raise RequestAdmissionError("llm_request_method_unsupported")
        if self.prompts is not None:
            from redteam.orchestration.prompts import instruction_selection

            stage, role_id = instruction_selection()
            kwargs["system"] = self.prompts.compose(
                stage, kwargs.get("system") or "", role_id=role_id,
            )
        # Shared Retry-After survives a new instance and process restart. Waiting
        # is cancellable and happens outside the coordinator transaction/slot.
        delay = await self.ledger.wait_seconds(scope.run_id)
        if delay:
            await self.sleep(delay)
        # Time/mission admission AFTER semaphore + cooldown wait, immediately
        # BEFORE reserve/dispatch. Covers strategy, atomization, repair and
        # code-role requests alike: they all pass through this one path and
        # charge the shared Run ledger below. A denial raises
        # RequestAdmissionError (a ReasoningHalted) so the decision client
        # converts it to a structured budget stop, never an empty success.
        # Feed path: an explicit admission_guard call kwarg, else the task-local
        # llm_request_guard scope bound by the owning planner/engine turn.
        guard = kwargs.pop("admission_guard", None)
        if guard is None:
            guard = _guard.get()
        if guard is not None:
            await guard()
        kwargs["num_retries"] = 0
        request = await self.ledger.reserve(scope.run_id, owner=scope.owner, kind=method,
                                            task_id=scope.task_id)
        # A failed durable dispatch update never starts the external request.
        # A possibly committed update is left reserved for explicit recovery.
        await self.ledger.dispatch(scope.run_id, request)
        try:
            response = await call(**kwargs)
        except BaseException as exc:
            # If settlement itself fails, the durable dispatched reservation
            # remains charged. Do not mask the original exception/cancellation.
            try:
                await self.ledger.settle(scope.run_id, request, unknown=True)
            except BaseException:
                pass
            if isinstance(exc, LLMProviderFailure):
                await self.ledger.record_provider_outcome(scope.run_id, exc)
            raise
        await self.ledger.settle(
            scope.run_id, request, total_tokens=getattr(response, "total_tokens", None),
        )
        await self.ledger.record_provider_outcome(scope.run_id)
        return response

    async def complete(self, *, messages, system=None, **kwargs) -> str:
        return await self._call("complete", messages=messages, system=system, **kwargs)

    async def complete_native(self, messages, tools, system=None, **kwargs):
        return await self._call(
            "complete_native", messages=messages, tools=tools, system=system, **kwargs,
        )

    async def complete_structured_with_usage(
        self, *, messages, response_schema, system=None, **kwargs,
    ) -> StructuredCompletion:
        metered = callable(getattr(self.client, "complete_structured_with_usage", None))
        method = "complete_structured_with_usage" if metered else "complete_structured"
        response = await self._call(
            method, messages=messages, response_schema=response_schema, system=system, **kwargs,
        )
        return response if metered else StructuredCompletion(value=response)

    async def complete_structured(self, *, messages, response_schema, system=None, **kwargs):
        response = await self.complete_structured_with_usage(
            messages=messages, response_schema=response_schema, system=system, **kwargs,
        )
        return json.loads(response.value) if isinstance(response.value, str) else response.value


def configure_request_metering(
    client: Any, settings: Any, coordinator: Any, *, prompts: AgentPrompts | None = None,
) -> Any:
    if settings.llm_request_policy is None:
        return client
    if isinstance(client, RunMeteredLLM):
        if client.ledger.coordinator.backend is not coordinator.backend:
            raise ValueError("llm_meter_coordinator_mismatch")
        if prompts is not None and client.prompts is not prompts:
            if client.prompts is not None:
                raise ValueError("llm_prompt_snapshot_mismatch")
            return RunMeteredLLM(client.client, client.ledger, sleep=client.sleep, prompts=prompts,
                                 request_semaphore=client.request_semaphore)
        return client
    return RunMeteredLLM(client, RunRequestLedger(coordinator), prompts=prompts)
