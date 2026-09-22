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

**Current status**: every event above is real and emitted in a normal
run, including `AgentFinished` (`orchestration.nodes.replan`) and
`HypothesisCreated` (`orchestration.nodes.plan`, whenever a step
declares a `HypothesisDraft` — every built-in playbook step does).
`FindingCreated`/`FindingVerified` are the one pair that still never
fires: `orchestration.nodes.verify` only publishes a `Finding` when a
step's `Expectation` reaches `Verdict.VIOLATION`
(`violation_if_met=True`), and no built-in playbook step sets that —
see `docs/agent/orchestration-implementation-plan.md` section 16.1,
finding 1. The vocabulary itself is fixed by PM either way, so nothing
needs inventing once a playbook actually sets one.

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
