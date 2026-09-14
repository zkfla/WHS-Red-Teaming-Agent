# Events

Schema: `redteam.core.contracts.events.EventEnvelope`. Backend:
`redteam.core.ports.event_bus.EventBus` (in-memory implementation:
`redteam.event_bus.memory.InMemoryEventBus`).

## Envelope

```python
class EventEnvelope(BaseModel):
    event_id: EventId
    event_type: str
    run_id: RunId
    engagement_id: EngagementId
    source: str
    timestamp: datetime
    correlation_id: str | None = None
    causation_id: str | None = None
    payload: dict[str, Any] = {}
```

`event_type` is a free string, but use the constants in
`redteam.core.contracts.events.EventType` rather than inventing new
literals — that's the shared vocabulary every team's subscribers key
off of.

## Canonical event types

```text
RunStarted           RunFinished
AgentStarted          AgentFinished
HypothesisCreated
ActionProposed
PolicyEvaluated
ActionApproved        ActionDenied         ApprovalRequired
ToolExecutionStarted  ToolExecutionFinished  ToolExecutionFailed
EvidenceCreated
GraphUpdated
FindingCreated        FindingVerified
```

Only `RunStarted`, `AgentStarted`, `ActionProposed`, `PolicyEvaluated`,
`ActionApproved`/`ActionDenied`/`ApprovalRequired`,
`ToolExecutionStarted`/`ToolExecutionFinished`/`ToolExecutionFailed`,
`EvidenceCreated`, `GraphUpdated`, and `RunFinished` are emitted by the
current stub flow. `AgentFinished`, `HypothesisCreated`,
`FindingCreated`, `FindingVerified` aren't yet (no real agent-lifecycle/
hypothesis/finding logic) — reserved for Agent Team's planner work; the
vocabulary is fixed by PM so there's nothing to invent when that lands.

## Subscribing

```python
async def handler(event: EventEnvelope) -> None:
    ...

await event_bus.subscribe("ActionProposed", handler)
await event_bus.subscribe("*", handler)  # every event
```

## Swapping the backend

Implement `redteam.core.ports.event_bus.EventBus` (`publish`,
`subscribe`) against Redis/NATS/Kafka and wire it in
`redteam.app.bootstrap.build_runtime_deps` — nothing else changes.
