"""One request facade shared by component factories and independent instances."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, is_dataclass
from typing import Any

from redteam.core.ports.agent_prompts import AgentPrompts
from redteam.core.ports.llm import LLMProviderFailure, StructuredCompletion
from redteam.core.request_scope import (
    RequestPurpose,
    RequestScope,
    clear_completed_request,
    current_request_metadata,
    current_request_scope,
    llm_request_scope,
    mark_completed_request,
)
from redteam.orchestration.llm_requests import (
    RequestAdmissionError,
    RunRequestLedger,
    outcome_for_exception,
)

__all__ = ["RequestScope", "current_request_scope", "llm_request_scope", "RunMeteredLLM",
           "configure_request_metering", "llm_request_guard", "current_request_guard"]


def _purpose_for(stage: str | None) -> RequestPurpose:
    """Classify from the code-owned call stage, never provider text."""
    if stage == "task_atomization":
        return "atomization"
    if stage == "native_action":
        return "worker_decision"
    if stage and ("planning" in stage or stage == "attack_path_proposal"):
        return "planning"
    if stage and ("review" in stage or "completion" in stage or "verify" in stage):
        return "review"
    if stage and "conversation" in stage:
        return "conversation"
    return "other"


_guard: ContextVar[Callable[[], Awaitable[None]] | None] = ContextVar(
    "llm_request_guard", default=None,
)

_PRIVATE_MODEL_KEYS = frozenset({
    "continuation", "encrypted_content", "reasoning", "signature", "thinking",
})


def _public_value(value: Any) -> Any:
    """Return JSON-shaped public material without provider-private state."""
    if is_dataclass(value) and not isinstance(value, type):
        value = asdict(value)
    elif callable(getattr(value, "model_dump", None)):
        value = value.model_dump(mode="json")
    if isinstance(value, dict):
        return {
            str(key): _public_value(item)
            for key, item in value.items()
            if str(key).lower() not in _PRIVATE_MODEL_KEYS
        }
    if isinstance(value, (list, tuple)):
        return [_public_value(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _request_payload(method: str, kwargs: dict[str, Any]) -> dict[str, Any]:
    """Whitelisted public prompt fields; transport credentials never enter it."""
    payload: dict[str, Any] = {
        "method": method,
        "system": _public_value(kwargs.get("system")),
        "messages": _public_value(kwargs.get("messages", [])),
    }
    for name in ("response_schema", "tools"):
        if name in kwargs:
            payload[name] = _public_value(kwargs[name])
    return payload


def _response_payload(method: str, response: Any) -> Any:
    """Only public text/JSON/tool calls; never hidden continuation data."""
    if method == "complete_native":
        return {
            "text": _public_value(getattr(response, "text", "")),
            "tool_calls": _public_value(getattr(response, "tool_calls", ())),
            "protocol_error": _public_value(getattr(response, "protocol_error", None)),
        }
    if isinstance(response, StructuredCompletion):
        return _public_value(response.value)
    return _public_value(response)


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
        self._completed_request_ids: dict[int, str] = {}

    def _remember_request(self, value: Any, request_id: str) -> None:
        self._completed_request_ids[id(value)] = request_id
        while len(self._completed_request_ids) > 256:
            self._completed_request_ids.pop(next(iter(self._completed_request_ids)))

    def request_id_for(self, value: Any) -> str | None:
        """Consume the row identity paired with this exact response/exception object."""
        return self._completed_request_ids.pop(id(value), None)

    async def _call(self, method: str, **kwargs: Any) -> Any:
        if self.request_semaphore is None:
            return await self._call_admitted(method, **kwargs)
        async with self.request_semaphore:
            return await self._call_admitted(method, **kwargs)

    async def _call_admitted(self, method: str, **kwargs: Any) -> Any:
        clear_completed_request()
        scope = current_request_scope()
        if scope is None:
            raise RequestAdmissionError("llm_request_run_scope_missing")
        call = getattr(self.client, method, None)
        if call is None:
            raise RequestAdmissionError("llm_request_method_unsupported")
        from redteam.orchestration.prompts import instruction_selection

        try:
            stage, role_id = instruction_selection()
        except ValueError:
            if self.prompts is not None:
                raise
            stage, role_id = None, None
        if self.prompts is not None:
            assert stage is not None
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
        metadata = current_request_metadata()
        purpose = metadata.purpose if metadata is not None else _purpose_for(stage)
        attempt_kind = metadata.attempt_kind if metadata is not None else "normal"
        request = await self.ledger.reserve(
            scope.run_id, owner=scope.owner, kind=method,
            task_id=scope.task_id, stage=stage, purpose=purpose,
            attempt_kind=attempt_kind,
            request_payload=_request_payload(method, kwargs),
        )
        if metadata is not None:
            metadata.request_id = request
        # The attempt timeout wraps this facade as well as the provider. If it
        # interrupts the durable dispatch boundary, atomically release a row
        # that never crossed dispatch or charge a committed dispatch as unknown.
        # In either case, do not let later decision classification mask the
        # original timeout with a row-state conflict.
        try:
            await self.ledger.dispatch(scope.run_id, request)
        except BaseException as exc:
            charged: bool | None = None
            try:
                charged = await self.ledger.settle_interrupted_dispatch(
                    scope.run_id, request, outcome=outcome_for_exception(exc),
                )
            except BaseException:
                pass
            if charged:
                self._remember_request(exc, request)
                mark_completed_request(scope.run_id, request)
            elif metadata is not None:
                metadata.request_id = None
            raise
        try:
            response = await call(**kwargs)
        except BaseException as exc:
            # Provider failures atomically settle the request and advance the
            # shared cooldown generation. Other failures/cancellation retain
            # the original unknown-settlement behavior.
            try:
                if isinstance(exc, LLMProviderFailure):
                    await self.ledger.settle_provider_outcome(
                        scope.run_id, request, failure=exc,
                        outcome=outcome_for_exception(exc),
                    )
                else:
                    await self.ledger.settle(
                        scope.run_id, request, unknown=True,
                        outcome=outcome_for_exception(exc),
                    )
            except BaseException:
                pass
            self._remember_request(exc, request)
            mark_completed_request(scope.run_id, request)
            raise
        await self.ledger.settle_provider_outcome(
            scope.run_id, request, total_tokens=getattr(response, "total_tokens", None),
            usage=getattr(response, "usage", None),
            response_payload=_response_payload(method, response),
        )
        self._remember_request(response, request)
        mark_completed_request(scope.run_id, request)
        return response

    async def classify_request(
        self,
        request_id: str,
        *,
        purpose: RequestPurpose | None = None,
        outcome: str,
        valid: bool,
        made_progress: bool | None = None,
    ) -> None:
        scope = current_request_scope()
        if scope is None:
            raise RequestAdmissionError("llm_request_run_scope_missing")
        await self.ledger.classify_decision(
            scope.run_id, request_id, purpose=purpose, outcome=outcome,
            valid=valid, made_progress=made_progress,
        )

    async def complete(self, *, messages, system=None, **kwargs) -> str:
        if callable(getattr(self.client, "complete_with_usage", None)):
            response = await self._call(
                "complete_with_usage", messages=messages, system=system, **kwargs,
            )
            return str(response.value)
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
