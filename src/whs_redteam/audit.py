"""Append-only JSONL audit events."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import Any

from whs_redteam.sanitizer import sanitize_data


class AuditLogger:
    """Write one structured and flush-safe event per line."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = Lock()

    def emit(self, event_type: str, **fields: Any) -> None:
        event = sanitize_data({
            "timestamp": datetime.now(UTC).isoformat(),
            "type": event_type,
            **fields,
        })
        serialized = json.dumps(event, ensure_ascii=False, sort_keys=True, default=str)
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(serialized + "\n")
                handle.flush()
