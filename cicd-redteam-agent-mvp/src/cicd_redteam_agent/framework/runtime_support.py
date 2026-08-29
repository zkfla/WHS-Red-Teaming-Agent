from __future__ import annotations

import asyncio
import ctypes
import json
import os
import random
import socket
import tempfile
import threading
import uuid
from collections.abc import Callable
from ctypes import wintypes
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from cicd_redteam_agent.domain.investigation import RedTeamSession
from cicd_redteam_agent.domain.models import AnalysisState

CHECKPOINT_SCHEMA_VERSION = "2.0"
INVESTIGATION_CHECKPOINT_SCHEMA_VERSION = "1.0"
EVENT_SCHEMA_VERSION = "1.0"
PROMPT_VERSION = "1.7"


class RecoverableModelOutputError(RuntimeError):
    """A provider completed a turn but returned malformed structured output."""


@dataclass
class AgentRuntimeMetrics:
    model_attempts: int = 0
    retries: int = 0
    retry_delay_seconds: float = 0.0
    context_compactions: int = 0
    omitted_history_messages: int = 0
    checkpoints_written: int = 0
    last_capability: str | None = None
    last_error_type: str | None = None
    no_progress_rounds: int = 0
    investigation_tasks_completed: int = 0
    observations_committed: int = 0
    semantic_refreshes: int = 0
    frontier_searches: int = 0
    external_network_requests: int = 0
    external_cache_hits: int = 0
    external_not_modified_hits: int = 0
    external_rate_limit_errors: int = 0
    external_retries: int = 0
    external_rate_limit_remaining: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _status_code(exc: Exception) -> int | None:
    for candidate in (exc, getattr(exc, "response", None)):
        value = getattr(candidate, "status_code", None) or getattr(candidate, "status", None)
        try:
            if value is not None:
                return int(value)
        except (TypeError, ValueError):
            continue
    return None


def is_transient_model_error(exc: Exception) -> bool:
    if isinstance(exc, RecoverableModelOutputError):
        return True
    status = _status_code(exc)
    if status is not None:
        return status in {408, 409, 425, 429} or status >= 500

    error_name = type(exc).__name__.lower()
    message = str(exc).lower()
    fatal_markers = (
        "authentication",
        "permission",
        "badrequest",
        "invalidrequest",
        "validation",
        "jsondecode",
        "unauthorized",
        "forbidden",
        "invalid api key",
        "context length",
        "exceeds the configured",
        "unknown capability",
    )
    if any(marker in error_name or marker in message for marker in fatal_markers):
        return False

    retryable_markers = (
        "timeout",
        "timed out",
        "connection",
        "ratelimit",
        "rate limit",
        "serviceunavailable",
        "service unavailable",
        "temporarily unavailable",
        "internalserver",
        "server error",
        "overloaded",
        "stream disconnected",
        "try again",
    )
    return isinstance(exc, (TimeoutError, ConnectionError)) or any(
        marker in error_name or marker in message for marker in retryable_markers
    )


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    initial_delay: float = 0.5
    max_delay: float = 8.0
    multiplier: float = 2.0

    def delay(self, retry_number: int, random_value: float | None = None) -> float:
        ceiling = min(
            self.max_delay,
            self.initial_delay * (self.multiplier ** max(0, retry_number - 1)),
        )
        sample = random.random() if random_value is None else random_value
        return max(0.0, ceiling * min(1.0, max(0.0, sample)))


async def call_model_with_retry[T](
    operation: Callable[[], T],
    *,
    policy: RetryPolicy,
    metrics: AgentRuntimeMetrics,
    time_remaining: Callable[[], timedelta | None],
    on_retry: Callable[[int, float, Exception], Any] | None = None,
    sleep: Callable[[float], Any] = asyncio.sleep,
) -> T:
    for attempt in range(1, policy.max_attempts + 1):
        metrics.model_attempts += 1
        try:
            return operation()
        except Exception as exc:
            metrics.last_error_type = type(exc).__name__
            if attempt >= policy.max_attempts or not is_transient_model_error(exc):
                raise
            delay = policy.delay(attempt)
            remaining = time_remaining()
            if remaining is not None and remaining.total_seconds() <= delay:
                raise
            metrics.retries += 1
            metrics.retry_delay_seconds += delay
            if on_retry is not None:
                callback_result = on_retry(attempt, delay, exc)
                if hasattr(callback_result, "__await__"):
                    await callback_result
            await sleep(delay)
    raise RuntimeError("retry loop exhausted without returning or raising")


def write_atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            json.dump(payload, temporary, ensure_ascii=False, indent=2, default=str)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, path)
    except Exception:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
        raise


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    return str(value)


@dataclass(frozen=True)
class AgentEvent:
    sequence: int
    event_id: str
    event_type: str
    attempt_id: int
    round: int
    timestamp: str
    payload: dict[str, Any]


class EventJournal:
    """Append-only execution history; structured analysis facts stay in AnalysisState."""

    def __init__(self, path: Path, *, attempt_id: int) -> None:
        self.path = path
        self.attempt_id = attempt_id
        self._lock = threading.Lock()
        self._events = self._load_and_repair_tail(path)
        self._sequence = self._events[-1].sequence if self._events else 0

    @staticmethod
    def _load_and_repair_tail(path: Path) -> list[AgentEvent]:
        if not path.exists():
            return []
        raw = path.read_bytes()
        lines = raw.splitlines(keepends=True)
        events: list[AgentEvent] = []
        valid_bytes = 0
        nonempty_indexes = [index for index, line in enumerate(lines) if line.strip()]
        last_nonempty = nonempty_indexes[-1] if nonempty_indexes else -1
        previous_sequence = 0
        for index, line in enumerate(lines):
            if not line.strip():
                valid_bytes += len(line)
                continue
            try:
                payload = json.loads(line)
                if payload.get("schema_version") != EVENT_SCHEMA_VERSION:
                    raise ValueError("unsupported event schema")
                event = AgentEvent(
                    sequence=int(payload["sequence"]),
                    event_id=str(payload["event_id"]),
                    event_type=str(payload["event_type"]),
                    attempt_id=int(payload["attempt_id"]),
                    round=int(payload.get("round", 0)),
                    timestamp=str(payload["timestamp"]),
                    payload=dict(payload.get("payload", {})),
                )
                if event.sequence != previous_sequence + 1:
                    raise ValueError("non-contiguous event sequence")
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                if index != last_nonempty:
                    raise ValueError(
                        f"execution journal is corrupt before its final line: {path}"
                    ) from exc
                EventJournal._truncate_tail(path, raw[:valid_bytes])
                break
            events.append(event)
            previous_sequence = event.sequence
            valid_bytes += len(line)
        return events

    @staticmethod
    def _truncate_tail(path: Path, valid_content: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
                temporary.write(valid_content)
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_path, path)
        finally:
            if temporary_path is not None and temporary_path.exists():
                temporary_path.unlink(missing_ok=True)

    @property
    def events(self) -> tuple[AgentEvent, ...]:
        return tuple(self._events)

    @property
    def last_sequence(self) -> int:
        return self._sequence

    def append(
        self,
        event_type: str,
        *,
        round: int = 0,
        payload: dict[str, Any] | None = None,
    ) -> AgentEvent:
        with self._lock:
            sequence = self._sequence + 1
            event_payload = {
                "schema_version": EVENT_SCHEMA_VERSION,
                "sequence": sequence,
                "event_id": f"evt_{uuid.uuid4().hex}",
                "event_type": event_type,
                "attempt_id": self.attempt_id,
                "round": round,
                "timestamp": datetime.now(UTC).isoformat(),
                "payload": _json_safe(payload or {}),
            }
            self.path.parent.mkdir(parents=True, exist_ok=True)
            needs_separator = self.path.exists() and self.path.stat().st_size > 0
            if needs_separator:
                with self.path.open("rb") as existing:
                    existing.seek(-1, os.SEEK_END)
                    needs_separator = existing.read(1) not in {b"\n", b"\r"}
            with self.path.open("a", encoding="utf-8", newline="\n") as stream:
                if needs_separator:
                    stream.write("\n")
                stream.write(json.dumps(event_payload, ensure_ascii=False) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            event = AgentEvent(
                sequence=sequence,
                event_id=event_payload["event_id"],
                event_type=event_type,
                attempt_id=self.attempt_id,
                round=round,
                timestamp=event_payload["timestamp"],
                payload=event_payload["payload"],
            )
            self._events.append(event)
            self._sequence = sequence
            return event


@dataclass(frozen=True)
class ResumeCheckpoint:
    path: Path
    state: AnalysisState
    provider: str
    model: str | None
    rounds: int
    tokens: int
    cost: float
    active_duration_seconds: float
    runtime_metrics: AgentRuntimeMetrics
    reason: str
    resume_phase: str
    committed_event_seq: int
    attempt_id: int
    prompt_version: str
    config: dict[str, Any]
    investigation_session: RedTeamSession | None = None


def load_checkpoint(path: Path) -> ResumeCheckpoint:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"checkpoint not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"checkpoint is not valid JSON: {path}") from exc
    if payload.get("schema_version") != CHECKPOINT_SCHEMA_VERSION:
        raise ValueError(
            "checkpoint cannot be resumed because its schema is not 2.0; start a new analysis run"
        )
    runtime_payload = dict(payload.get("runtime_metrics", {}))
    metric_fields = AgentRuntimeMetrics.__dataclass_fields__
    runtime_metrics = AgentRuntimeMetrics(
        **{key: value for key, value in runtime_payload.items() if key in metric_fields}
    )
    state = AnalysisState.model_validate(payload["state"])
    config = dict(payload.get("config", {}))
    raw_session = payload.get("investigation_session")
    investigation_session = None
    if raw_session is not None:
        if not isinstance(raw_session, dict) or raw_session.get("schema_version") != (
            INVESTIGATION_CHECKPOINT_SCHEMA_VERSION
        ):
            raise ValueError(
                "checkpoint investigation session schema is unsupported; start a new analysis run"
            )
        investigation_session = RedTeamSession.model_validate(raw_session.get("session"))
    if bool(config.get("observe_github", False)) and investigation_session is None:
        raise ValueError(
            "checkpoint enabled GitHub observation but has no investigation session; "
            "start a new analysis run"
        )
    if investigation_session is not None and not bool(config.get("observe_github", False)):
        raise ValueError(
            "checkpoint contains an investigation session without GitHub observation enabled"
        )
    if investigation_session is not None:
        if investigation_session.investigation_state.active_lease is not None:
            raise ValueError("checkpoint contains an unsafe active investigation lease")
        if investigation_session.analysis_state.model_dump(mode="json", by_alias=True) != (
            state.model_dump(mode="json", by_alias=True)
        ):
            raise ValueError(
                "checkpoint investigation session does not match the canonical analysis state"
            )
    return ResumeCheckpoint(
        path=path,
        state=state,
        provider=str(payload["provider"]),
        model=payload.get("model"),
        rounds=int(payload.get("rounds", 0)),
        tokens=int(payload.get("tokens", 0)),
        cost=float(payload.get("cost", 0.0)),
        active_duration_seconds=float(payload.get("active_duration_seconds", 0.0)),
        runtime_metrics=runtime_metrics,
        reason=str(payload.get("reason", "unknown")),
        resume_phase=str(payload.get("resume_phase", "BETWEEN_ROUNDS")),
        committed_event_seq=int(payload.get("committed_event_seq", 0)),
        attempt_id=int(payload.get("attempt_id", 1)),
        prompt_version=str(payload.get("prompt_version", "")),
        config=config,
        investigation_session=investigation_session,
    )


def restore_limits(limits: Any, checkpoint: ResumeCheckpoint) -> None:
    limits._rounds = checkpoint.rounds
    limits._tokens = checkpoint.tokens
    limits._cost = checkpoint.cost
    limits._reason = None
    limits._start_time = datetime.now() - timedelta(seconds=checkpoint.active_duration_seconds)


class RunLease:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._acquired = False

    @staticmethod
    def _process_alive(pid: int) -> bool:
        if pid <= 0:
            return False
        if os.name == "nt":
            process_query_limited_information = 0x1000
            still_active = 259
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, wintypes.LPDWORD]
            kernel32.GetExitCodeProcess.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL
            handle = kernel32.OpenProcess(
                process_query_limited_information,
                False,
                pid,
            )
            if not handle:
                return ctypes.get_last_error() == 5
            try:
                exit_code = wintypes.DWORD()
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    return False
                return exit_code.value == still_active
            finally:
                kernel32.CloseHandle(handle)
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True

    def __enter__(self) -> RunLease:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            try:
                current = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                live = False
            else:
                same_host = current.get("host") == socket.gethostname()
                if not same_host:
                    raise ValueError(
                        "analysis run lock belongs to another host and cannot be "
                        f"safely replaced: {self.path.parent}"
                    )
                try:
                    live = self._process_alive(int(current.get("pid", 0)))
                except (TypeError, ValueError):
                    live = False
            if live:
                raise ValueError(f"analysis run is already active: {self.path.parent}")
            self.path.unlink(missing_ok=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        descriptor = os.open(self.path, flags, 0o600)
        try:
            payload = {
                "pid": os.getpid(),
                "host": socket.gethostname(),
                "created_at": datetime.now(UTC).isoformat(),
            }
            os.write(descriptor, json.dumps(payload).encode("utf-8"))
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        self._acquired = True
        return self

    def __exit__(self, *_args: object) -> None:
        if self._acquired:
            self.path.unlink(missing_ok=True)
            self._acquired = False


@dataclass
class CheckpointWriter:
    path: Path
    state: AnalysisState
    provider: str
    model: str | None
    metrics: AgentRuntimeMetrics
    journal: EventJournal | None = None
    attempt_id: int = 1
    config: dict[str, Any] | None = None
    investigation_session: RedTeamSession | None = None

    def write(
        self,
        *,
        rounds: int,
        tokens: int,
        cost: float,
        reason: str,
        active_duration_seconds: float = 0.0,
    ) -> None:
        observe_github = bool((self.config or {}).get("observe_github", False))
        if observe_github and self.investigation_session is None:
            raise ValueError("GitHub observation checkpoint requires an investigation session")
        session_payload: dict[str, Any] | None = None
        if self.investigation_session is not None:
            if self.investigation_session.investigation_state.active_lease is not None:
                raise ValueError("cannot checkpoint while an investigation task lease is active")
            session_snapshot = self.investigation_session.model_copy(deep=True)
            session_snapshot.analysis_state = AnalysisState.model_validate(
                self.state.model_dump(mode="json", by_alias=True)
            )
            session_snapshot = RedTeamSession.model_validate(
                session_snapshot.model_dump(mode="json", by_alias=True)
            )
            session_payload = {
                "schema_version": INVESTIGATION_CHECKPOINT_SCHEMA_VERSION,
                "session": session_snapshot.model_dump(mode="json", by_alias=True),
            }
        next_checkpoint_count = self.metrics.checkpoints_written + 1
        runtime_metrics = self.metrics.to_dict()
        runtime_metrics["checkpoints_written"] = next_checkpoint_count
        resume_phase = "TERMINAL" if reason == "proposal_validated" else "BETWEEN_ROUNDS"
        committed_event_seq = 0
        if self.journal is not None:
            event = self.journal.append(
                "CHECKPOINT_COMMITTED",
                round=rounds,
                payload={"reason": reason, "resume_phase": resume_phase},
            )
            committed_event_seq = event.sequence
        write_atomic_json(
            self.path,
            {
                "schema_version": CHECKPOINT_SCHEMA_VERSION,
                "updated_at": datetime.now(UTC).isoformat(),
                "reason": reason,
                "resume_phase": resume_phase,
                "committed_event_seq": committed_event_seq,
                "attempt_id": self.attempt_id,
                "provider": self.provider,
                "model": self.model,
                "rounds": rounds,
                "tokens": tokens,
                "cost": cost,
                "active_duration_seconds": active_duration_seconds,
                "runtime_metrics": runtime_metrics,
                "prompt_version": PROMPT_VERSION,
                "config": _json_safe(self.config or {}),
                "state": self.state.model_dump(mode="json", by_alias=True),
                "investigation_session": session_payload,
            },
        )
        self.metrics.checkpoints_written = next_checkpoint_count
