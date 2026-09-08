from __future__ import annotations

import json
import os
import tempfile
import threading
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from pydantic import ConfigDict, Field, model_validator

from redteam_core.models.base import SchemaModel, canonical_json, sha256_text, stable_id
from redteam_core.models.execution import ExperimentAttempt, ExperimentDescriptor, ExperimentResult
from redteam_core.runtime.clock import RuntimeClock

EVENT_SCHEMA_VERSION = "2.0"


class RuntimeEventType(StrEnum):
    OPERATIONAL_DISPATCH_INTENT = "OPERATIONAL_DISPATCH_INTENT"
    OPERATIONAL_REQUEST_SETTLED = "OPERATIONAL_REQUEST_SETTLED"
    RUN_STARTED = "RUN_STARTED"
    RESUME_STARTED = "RESUME_STARTED"
    CHECKPOINT_WRITTEN = "CHECKPOINT_WRITTEN"
    EXPERIMENT_EFFECT_STARTED = "EXPERIMENT_EFFECT_STARTED"
    EXPERIMENT_EFFECT_TERMINAL = "EXPERIMENT_EFFECT_TERMINAL"
    CLEANUP_EFFECT_STARTED = "CLEANUP_EFFECT_STARTED"
    CLEANUP_EFFECT_TERMINAL = "CLEANUP_EFFECT_TERMINAL"
    FINALIZATION_STARTED = "FINALIZATION_STARTED"
    RUN_FINALIZED = "RUN_FINALIZED"


class RuntimeEvent(SchemaModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True, frozen=True)

    schema_version: str = EVENT_SCHEMA_VERSION
    sequence: int = Field(ge=1)
    event_id: str = Field(pattern=r"^event_[0-9a-f]{16}$")
    run_id: str = Field(min_length=1, max_length=200)
    event_type: RuntimeEventType
    timestamp: datetime
    attributes: tuple[tuple[str, str], ...] = Field(default=(), max_length=128)
    previous_event_hash: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    event_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    def _hash_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "sequence": self.sequence,
            "event_id": self.event_id,
            "run_id": self.run_id,
            "event_type": self.event_type.value,
            "timestamp": self.timestamp.isoformat(),
            "attributes": [list(item) for item in self.attributes],
            "previous_event_hash": self.previous_event_hash,
        }

    @model_validator(mode="after")
    def validate_event(self) -> RuntimeEvent:
        if self.schema_version != EVENT_SCHEMA_VERSION:
            raise ValueError("unsupported runtime event schema")
        if self.timestamp.tzinfo is None or self.timestamp.utcoffset() is None:
            raise ValueError("runtime event timestamp must be timezone-aware")
        if tuple(sorted(self.attributes)) != self.attributes:
            raise ValueError("runtime event attributes must be canonically sorted")
        attribute_keys = [key for key, _ in self.attributes]
        if len(set(attribute_keys)) != len(attribute_keys):
            raise ValueError("runtime event attribute keys must be unique")
        expected_id = stable_id(
            "event",
            self.run_id,
            self.sequence,
            self.event_type.value,
            self.timestamp.isoformat(),
            canonical_json([list(item) for item in self.attributes]),
            self.previous_event_hash or "",
        )
        if self.event_id != expected_id:
            raise ValueError("runtime event_id does not match event content")
        expected_hash = "sha256:" + sha256_text(canonical_json(self._hash_payload()))
        if self.event_hash != expected_hash:
            raise ValueError("runtime event hash mismatch")
        return self

    def attribute(self, key: str, default: str = "") -> str:
        for item_key, value in self.attributes:
            if item_key == key:
                return value
        return default

    @classmethod
    def create(
        cls,
        *,
        sequence: int,
        run_id: str,
        event_type: RuntimeEventType,
        timestamp: datetime,
        attributes: dict[str, str] | None = None,
        previous_event_hash: str | None = None,
    ) -> RuntimeEvent:
        normalized = tuple(sorted((attributes or {}).items()))
        event_id = stable_id(
            "event",
            run_id,
            sequence,
            event_type.value,
            timestamp.isoformat(),
            canonical_json([list(item) for item in normalized]),
            previous_event_hash or "",
        )
        payload = {
            "schema_version": EVENT_SCHEMA_VERSION,
            "sequence": sequence,
            "event_id": event_id,
            "run_id": run_id,
            "event_type": event_type.value,
            "timestamp": timestamp.isoformat(),
            "attributes": [list(item) for item in normalized],
            "previous_event_hash": previous_event_hash,
        }
        return cls(
            **payload,
            event_hash="sha256:" + sha256_text(canonical_json(payload)),
        )


class EventJournal:
    """Fsync'd append-only operational journal with a deterministic hash chain."""

    def __init__(self, path: Path, *, run_id: str, clock: RuntimeClock) -> None:
        self.path = Path(path)
        self.run_id = run_id
        self.clock = clock
        self._lock = threading.Lock()
        self._events = self._load_and_repair_tail(self.path)
        if any(event.run_id != run_id for event in self._events):
            raise ValueError("runtime journal belongs to a different run_id")

    @staticmethod
    def _atomic_replace(path: Path, content: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as stream:
                temporary_path = Path(stream.name)
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, path)
        finally:
            if temporary_path is not None and temporary_path.exists():
                temporary_path.unlink(missing_ok=True)

    @classmethod
    def _load_and_repair_tail(cls, path: Path) -> list[RuntimeEvent]:
        if not path.exists():
            return []
        raw = path.read_bytes()
        lines = raw.splitlines(keepends=True)
        nonempty = [index for index, line in enumerate(lines) if line.strip()]
        last_nonempty = nonempty[-1] if nonempty else -1
        valid_bytes = 0
        events: list[RuntimeEvent] = []
        previous_hash: str | None = None
        previous_sequence = 0
        for index, line in enumerate(lines):
            if not line.strip():
                valid_bytes += len(line)
                continue
            try:
                raw_event = json.loads(line)
            except json.JSONDecodeError as exc:
                if index != last_nonempty:
                    raise ValueError("runtime journal is corrupt before its final line") from exc
                cls._atomic_replace(path, raw[:valid_bytes])
                break
            try:
                event = RuntimeEvent.model_validate(raw_event)
            except Exception as exc:
                raise ValueError("runtime journal contains a semantically invalid event") from exc
            if event.sequence != previous_sequence + 1:
                raise ValueError("runtime journal event sequence is not contiguous")
            if event.previous_event_hash != previous_hash:
                raise ValueError("runtime journal hash chain is broken")
            events.append(event)
            previous_hash = event.event_hash
            previous_sequence = event.sequence
            valid_bytes += len(line)
        return events

    @property
    def events(self) -> tuple[RuntimeEvent, ...]:
        return tuple(self._events)

    @property
    def last_sequence(self) -> int:
        return self._events[-1].sequence if self._events else 0

    @property
    def head_hash(self) -> str | None:
        return self._events[-1].event_hash if self._events else None

    def event_at(self, sequence: int) -> RuntimeEvent | None:
        if sequence <= 0 or sequence > len(self._events):
            return None
        candidate = self._events[sequence - 1]
        return candidate if candidate.sequence == sequence else None

    def events_after(self, sequence: int) -> tuple[RuntimeEvent, ...]:
        return tuple(event for event in self._events if event.sequence > sequence)

    def append(
        self,
        event_type: RuntimeEventType,
        *,
        attributes: dict[str, str] | None = None,
    ) -> RuntimeEvent:
        with self._lock:
            event = RuntimeEvent.create(
                sequence=self.last_sequence + 1,
                run_id=self.run_id,
                event_type=event_type,
                timestamp=self.clock.now(),
                attributes=attributes,
                previous_event_hash=self.head_hash,
            )
            self.path.parent.mkdir(parents=True, exist_ok=True)
            serialized = canonical_json(event.model_dump(mode="json")) + "\n"
            with self.path.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(serialized)
                stream.flush()
                os.fsync(stream.fileno())
            self._events.append(event)
            return event


class JournalExperimentEffectObserver:
    def __init__(self, journal: EventJournal) -> None:
        self.journal = journal

    def effect_started(
        self,
        *,
        attempt: ExperimentAttempt,
        descriptor: ExperimentDescriptor,
    ) -> None:
        self.journal.append(
            RuntimeEventType.EXPERIMENT_EFFECT_STARTED,
            attributes={
                "attempt_id": attempt.attempt_id,
                "attempt_sequence": str(attempt.sequence),
                "experiment_id": descriptor.experiment_id,
                "permit_id": attempt.permit_id or "",
            },
        )

    def effect_terminal(
        self,
        *,
        attempt: ExperimentAttempt,
        result: ExperimentResult | None,
    ) -> None:
        self.journal.append(
            RuntimeEventType.EXPERIMENT_EFFECT_TERMINAL,
            attributes={
                "attempt_id": attempt.attempt_id,
                "status": attempt.status.value,
                "result_id": result.result_id if result is not None else "",
            },
        )
