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

**Current status**: every event above is emitted by the corresponding
runtime path when its condition is met, including `AgentFinished`,
`HypothesisCreated`, and evidence-gated `FindingCreated`/`FindingVerified`.
The latter still requires a verified violation with evidence and a
confirmed hypothesis; observation-only results do not emit a Finding.
Agent-owned lifecycle extensions such as `CandidateRejected`,
`GraphMutationApplied`, `ResultMapperFailed`, `HypothesisResolved`, and
`ReplanTriggered` are documented in `docs/agent/event-bus.md` and remain
plain `EventEnvelope.event_type` strings so the frozen Core vocabulary is
not changed.

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
