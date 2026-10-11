"""One request facade shared by component factories and independent instances."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from redteam.core.ports.agent_prompts import AgentPrompts
from redteam.core.ports.llm import LLMProviderFailure, StructuredCompletion
from redteam.orchestration.llm_requests import RequestAdmissionError, RunRequestLedger


@dataclass(frozen=True)
class RequestScope:
    run_id: str
    owner: str


_scope: ContextVar[RequestScope | None] = ContextVar("llm_request_scope", default=None)


@contextmanager
def llm_request_scope(run_id: str, owner: str) -> Iterator[None]:
    token = _scope.set(RequestScope(run_id, owner))
    try:
        yield
    finally:
        _scope.reset(token)


def current_request_scope() -> RequestScope | None:
    """Read-only lifecycle identity for coordinated writes; never prompt data."""
    return _scope.get()


class RunMeteredLLM:
    """SDK retries are disabled; each explicit retry reserves a new request.

    Used only for configured request policies. Missing Run scope fails closed,
    including component calls accidentally made outside the run lifecycle.
    The adapter must honor num_retries=0 and issue one provider request per call.
    """

    def __init__(
        self, client: Any, ledger: RunRequestLedger, *, sleep=asyncio.sleep,
        prompts: AgentPrompts | None = None,
    ) -> None:
        self.client = client
        self.ledger = ledger
        self.sleep = sleep
        self.prompts = prompts

    async def _call(self, method: str, **kwargs: Any) -> Any:
        scope = _scope.get()
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
        kwargs["num_retries"] = 0
        request = await self.ledger.reserve(scope.run_id, owner=scope.owner, kind=method)
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
            return RunMeteredLLM(client.client, client.ledger, sleep=client.sleep, prompts=prompts)
        return client
    return RunMeteredLLM(client, RunRequestLedger(coordinator), prompts=prompts)
