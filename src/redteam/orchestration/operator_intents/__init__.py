"""Orchestration Operator Intent channel (runtime, not just contracts).

Public surface the composition root wires:

- ``OperatorIntentService(store=..., task_graph_store=..., event_bus=...)``
  or ``OperatorIntentService.from_deps(deps)``.
- ``InMemoryOperatorIntentStore`` / ``SQLiteOperatorIntentStore``, both
  satisfying the ``OperatorIntentStore`` Protocol.
- ``build_operator_intent_store(backend="memory"|"sqlite", path=...)``.
- ``interpret_instruction`` / ``classify_instruction`` for direct use.
- read-only ``status`` / ``explain`` methods on the service.

OBSERVE reaches the channel through ``deps.operator_intent_store`` (an
optional ``RuntimeDeps`` field the composition root adds); when the field
is absent, ``from_deps`` returns ``None`` and the run is unchanged.
"""

from redteam.orchestration.operator_intents.interpreter import (
    classify_instruction,
    extract_areas,
    interpret_instruction,
)
from redteam.orchestration.operator_intents.models import (
    IntentApplication,
    IntentClassification,
    IntentDrainResult,
    IntentExplanation,
    IntentInterpretationError,
    IntentRecord,
    IntentStatus,
    IntentStatusEntry,
    IntentStatusReport,
)
from redteam.orchestration.operator_intents.redaction import (
    REDACTED,
    is_secret_key,
    redact_mapping,
    redact_text,
    secret_parameters,
)
from redteam.orchestration.operator_intents.service import (
    FOCUS_PRIORITY,
    OperatorIntentService,
)
from redteam.orchestration.operator_intents.store import (
    DuplicateIntentError,
    InMemoryOperatorIntentStore,
    IntentNotFoundError,
    OperatorIntentStore,
    SQLiteOperatorIntentStore,
)


def build_operator_intent_store(
    backend: str = "memory", path: str | None = None
) -> OperatorIntentStore:
    """Construct the intent history store named by ``backend``.

    ``"sqlite"`` requires ``path``; any unknown value falls back to
    in-memory so a config typo cannot silently start persisting.
    """
    if backend == "sqlite" and path:
        return SQLiteOperatorIntentStore(path)
    return InMemoryOperatorIntentStore()


__all__ = [
    "FOCUS_PRIORITY",
    "REDACTED",
    "DuplicateIntentError",
    "InMemoryOperatorIntentStore",
    "IntentApplication",
    "IntentClassification",
    "IntentDrainResult",
    "IntentExplanation",
    "IntentInterpretationError",
    "IntentNotFoundError",
    "IntentRecord",
    "IntentStatus",
    "IntentStatusEntry",
    "IntentStatusReport",
    "OperatorIntentService",
    "OperatorIntentStore",
    "SQLiteOperatorIntentStore",
    "build_operator_intent_store",
    "classify_instruction",
    "extract_areas",
    "interpret_instruction",
    "is_secret_key",
    "redact_mapping",
    "redact_text",
    "secret_parameters",
]
