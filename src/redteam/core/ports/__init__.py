"""Port interfaces (Protocols) that every backend implementation satisfies.

Modules outside ``core`` depend on these Protocols, never on each
other's concrete classes. Bootstrap (``redteam.app.bootstrap``) is the
only place concrete implementations get wired to these ports.
"""

from redteam.core.ports.attack_graph import AttackGraphStore
from redteam.core.ports.control_plane import ControlPlane
from redteam.core.ports.event_bus import EventBus, EventHandler
from redteam.core.ports.evidence_store import EvidenceStore
from redteam.core.ports.llm import LLMClient
from redteam.core.ports.task_graph import MissionStore, MissionTaskStore, TaskGraphStore
from redteam.core.ports.tool_catalog import ToolCatalog
from redteam.core.ports.tool_gateway import ToolGateway

__all__ = [
    "AttackGraphStore",
    "ControlPlane",
    "EvidenceStore",
    "EventBus",
    "EventHandler",
    "LLMClient",
    "ToolGateway",
    "ToolCatalog",
    "MissionStore",
    "MissionTaskStore",
    "TaskGraphStore",
]
