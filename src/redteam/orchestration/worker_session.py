"""Task-local transcript foundation, not an execution or completion authority.

The react loop consumes this JSON contract in the existing checkpoint.
EvidenceStore and execution records remain canonical. Never log
the transcript or treat a restored binding as current Control permission.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from redteam.core.contracts.completion import completion_contract_hash
from redteam.core.contracts.mission import MissionTask, redact_secret_material
from redteam.core.ports.llm import NativeCompletion, NativeToolSpec
from redteam.orchestration.coordination import content_hash
from redteam.orchestration.state import RuntimeState


class SessionBinding(BaseModel):
    """Original admitted inputs, represented by existing IDs and fingerprints.

    Recovery may change the ownership epoch, so that epoch is not session
    identity. The owning activation path must still validate current ownership.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True, hide_input_in_errors=True)

    run_id: str = Field(min_length=1)
    engagement_id: str = Field(min_length=1)
    mission_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    instance_id: str | None = Field(min_length=1)
    purpose_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    candidate_binding_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    prompt_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    tools_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    provider_binding_hash: str = Field(pattern=r"^[a-f0-9]{64}$")

    @classmethod
    def capture(
        cls, runtime: RuntimeState, task: MissionTask, *, system: str,
        tools: list[NativeToolSpec], provider_binding: dict[str, Any],
    ) -> SessionBinding:
        """Use admitted Task/snapshot and selected final prompt/tools, not latest candidates."""
        if (runtime.mission_id is None
                or runtime.active_task_id != task.task_id or runtime.mission_id != task.mission_id):
            raise ValueError("worker session requires matching admitted task and instance")
        snapshots = runtime.probe_hypotheses or {}
        revisions = {key: value.get("revision") for key, value in snapshots.items()}
        if revisions != task.hypothesis_revisions:
            raise ValueError("worker session candidate revisions mismatch")
        return cls(
            run_id=str(runtime.run_id), engagement_id=str(runtime.engagement_id),
            mission_id=str(runtime.mission_id), task_id=str(task.task_id),
            instance_id=str(runtime.instance_id) if runtime.instance_id is not None else None,
            purpose_hash=completion_contract_hash(task),
            candidate_binding_hash=content_hash(runtime.probe_hypotheses),
            prompt_hash=content_hash(system), tools_hash=content_hash([asdict(t) for t in tools]),
            provider_binding_hash=content_hash(provider_binding),
        )

    @property
    def identity(self) -> tuple[str, str, str, str, str | None]:
        return (self.run_id, self.engagement_id, self.mission_id, self.task_id, self.instance_id)

    @property
    def prefix_identity(self) -> str:
        """Reusable prefix metadata, never conversation identity or a cache-hit claim."""
        return content_hash((self.prompt_hash, self.tools_hash, self.provider_binding_hash))


class FrozenMessage(BaseModel):
    """Exact first-delivered masked display, including window/provenance metadata.

    JSON text is immutable; decode returns a fresh copy. Selection/truncation is
    the evidence projection's job BEFORE capture, never recalculated on replay.
    Opaque continuation is carried unchanged under the existing native contract.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True, hide_input_in_errors=True)
    display_json: str = Field(repr=False)

    @field_validator("display_json")
    @classmethod
    def _validate_display(cls, value: str) -> str:
        message = json.loads(value)
        if (not isinstance(message, dict)
                or message.get("role") not in {"system", "user", "assistant", "tool"}
                or (message.get("content") is not None and not isinstance(message["content"], str))
                or set(message) - {"role", "content", "name", "tool_call_id", "tool_calls",
                                   "provider_continuation"}):
            raise ValueError("invalid public transcript message")
        # Reject non-finite numbers, including inside opaque continuation.
        json.dumps(message, allow_nan=False)
        if "provider_continuation" in message:
            if message["role"] != "assistant":
                raise ValueError("continuation requires an assistant message")
            NativeCompletion(continuation=message["provider_continuation"])
        return value

    @classmethod
    def capture(cls, message: dict[str, Any], *, max_content_chars: int) -> FrozenMessage:
        if type(max_content_chars) is not int or max_content_chars < 1:
            raise ValueError("display requires an explicit positive bound")
        visible = json.loads(json.dumps(
            {key: value for key, value in message.items() if key != "provider_continuation"},
            ensure_ascii=False, allow_nan=False,
        ))
        # Mask structured tool bodies before serialization, preserving already
        # selected offsets/limits. Never slice a JSON message into invalid JSON.
        def mask_json_text(content):
            if not isinstance(content, str):
                return content
            try:
                parsed = json.loads(content)
            except (ValueError, TypeError):
                return content
            return json.dumps(redact_secret_material(parsed), ensure_ascii=False, allow_nan=False)

        if "content" in visible:
            visible["content"] = mask_json_text(visible["content"])
        for call in visible.get("tool_calls") or []:
            function = call.get("function")
            if isinstance(function, dict) and "arguments" in function:
                function["arguments"] = mask_json_text(function["arguments"])
        call_ids = [call.get("id") for call in visible.get("tool_calls") or []]
        visible = redact_secret_material(visible)
        # Some SDKs encode an opaque signature IN the call ID. These protocol
        # identifiers must match the corresponding result byte-for-byte, just
        # like provider_continuation; only content/arguments are public text.
        for call, identity in zip(visible.get("tool_calls") or [], call_ids, strict=True):
            if identity is not None:
                call["id"] = identity
        if "tool_call_id" in message:
            visible["tool_call_id"] = message["tool_call_id"]
        content = visible.get("content")
        if content is not None and (
            not isinstance(content, str) or len(content) > max_content_chars
        ):
            raise ValueError("select a bounded public display before capture")
        if len(json.dumps(visible, ensure_ascii=False)) > max_content_chars:
            raise ValueError("select a bounded public display before capture")
        if "provider_continuation" in message:
            visible["provider_continuation"] = list(NativeCompletion(
                continuation=message["provider_continuation"],
            ).continuation)
        return cls(display_json=json.dumps(visible, ensure_ascii=False, allow_nan=False))

    def message(self) -> dict[str, Any]:
        return json.loads(self.display_json)


class SessionArchive(BaseModel):
    """An exact previous display generation in the same checkpoint, never replayed."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True, hide_input_in_errors=True)
    generation: int = Field(ge=0)
    reason: Literal["input_limit", "purpose_changed"]
    binding: SessionBinding
    messages: tuple[FrozenMessage, ...] = Field(repr=False)
    tools_json: str = Field(repr=False)
    input_bytes_before: int = Field(ge=0)
    input_bytes_after: int = Field(ge=0)


class WorkerSession(BaseModel):
    """JSON inside the existing instance checkpoint; no separate session store.

    Lifecycle labels are transcript metadata, not canonical task/run transitions.
    Generation identifies an explicit compaction/purpose boundary, never a turn.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True, hide_input_in_errors=True)
    version: Literal[2] = 2
    binding: SessionBinding
    generation: int = Field(default=0, ge=0)
    status: Literal["open", "waiting", "closed"] = "open"
    wait_reason: str | None = None
    messages: tuple[FrozenMessage, ...] = Field(default=(), repr=False)
    # Raw code-owned contract; the existing trusted prompt composer applies
    # common/role instructions once at dispatch. binding hashes that final text.
    system: str | None = Field(default=None, repr=False)
    tools_json: str = Field(default="[]", repr=False)
    initial_tool_count: int = Field(default=0, ge=0)
    notifications: tuple[tuple[str, str], ...] = ()
    archives: tuple[SessionArchive, ...] = Field(default=(), repr=False)

    @model_validator(mode="after")
    def _waiting_reason(self) -> WorkerSession:
        if (self.status == "waiting") != bool(self.wait_reason and self.wait_reason.strip()):
            raise ValueError("only waiting sessions require a wait reason")
        if (self.generation != len(self.archives)
                or any(archive.generation != index
                       or archive.binding.identity != self.binding.identity
                       for index, archive in enumerate(self.archives))):
            raise ValueError("worker session generation archive mismatch")
        return self

    def append(self, *messages: FrozenMessage) -> WorkerSession:
        if self.status != "open":
            raise ValueError("session is not open for model turns")
        return self.model_copy(update={"messages": (*self.messages, *messages)})

    def tools(self) -> list[NativeToolSpec]:
        return [NativeToolSpec(**item) for item in json.loads(self.tools_json)]

    def conversation(self) -> list[dict[str, Any]]:
        return [message.message() for message in self.messages]

    def checkpoint(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    @classmethod
    def restore(cls, checkpoint: dict[str, Any], *, expected: SessionBinding) -> WorkerSession:
        # JSON-mode validation accepts JSON arrays for frozen tuple fields.
        session = cls.model_validate_json(json.dumps(checkpoint, allow_nan=False))
        if session.binding != expected:
            raise ValueError("worker session binding mismatch")
        return session
