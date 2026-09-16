# Event Bus — implementation

Code: `src/redteam/event_bus/`. Schema: `core.contracts.events`
(`EventEnvelope`, `EventType`). Port: `core.ports.event_bus.EventBus`.
The contract side is documented in `docs/contracts/events.md`.

## Producing events

Every publisher needs the same six fields, and hand-assembling them at
each call site is how correlation ids quietly go missing — which is
exactly what makes a run impossible to reconstruct afterwards.
`EventPublisher` binds a run and a source and fills them in:

```python
publisher = EventPublisher(bus, run_id=run_id, engagement_id=eng_id,
                           source="orchestration.plan")

proposed = await publisher.publish(EventType.ACTION_PROPOSED,
                                   {"action_id": action_id},
                                   correlation_id=str(action_id))
await publisher.publish(EventType.POLICY_EVALUATED, {...},
                        correlation_id=str(action_id),
                        causation_id=proposed.event_id)
```

- **`correlation_id`** — what the event is *about*. Every event from
  `ActionProposed` through `GraphUpdated` carries the `ActionId`, so one
  action's full trail is a single filter. Defaults to the `run_id`.
- **`causation_id`** — the event that directly led to this one, so the
  chain can be walked backwards. Orchestration threads it through
  `GraphState["last_event_id"]`.

Commands are not events. This publishes facts that already happened;
asking another component to *do* something stays a direct call through a
`core` port.

## Delivery guarantees

`InMemoryEventBus`:

- **Order** — `publish` awaits delivery, so a single-task producer sees
  its events arrive in the order it published them. The integration
  tests depend on this.
- **Isolation** — a handler that raises is logged and counted
  (`handler_failures`); it cannot stop the other handlers or fail the
  publisher. This includes synchronous exceptions when calling a handler,
  before it returns an awaitable. A broken subscriber must not be able to abort a run.
- **Bounded history** — `history()`, `event_types()`,
  `correlated(id)` for debugging and assertions. This is a ring buffer,
  **not** durable storage; the Evidence Store keeps the record that
  matters. It is also in-memory-backend-only, not part of the port.

`"*"` subscribes to everything (see `app.cli`).
Subscriptions are not deduplicated: repeated registrations and registering the
same handler for both an event type and `"*"` each cause a separate invocation.

`InMemoryEventBus.subscription_count()` is a backend-only debugging and test
convenience. It reports registrations, so duplicate handlers are counted
separately; it is not part of the backend-independent `EventBus` port.

History is appended before handler execution and is returned oldest first.
Handlers for one publication start concurrently, and `publish()` waits for all
of them to finish. This backend has no retry, acknowledgement, durable
storage, or replay. Its history is a bounded in-memory debugging buffer; the
Evidence Store remains the durable record.

## What orchestration emits today

Happy path, one turn, in order:

```text
RunStarted             app.bootstrap
AgentStarted           orchestration.observe   (once per agent)
HypothesisCreated      orchestration.plan      (when the step states one)
ActionProposed         orchestration.plan
PolicyEvaluated        orchestration.policy
ActionApproved         orchestration.policy    (or ActionDenied / ApprovalRequired)
ToolExecutionStarted   orchestration.execute
ToolExecutionFinished  orchestration.execute   (or ToolExecutionFailed)
EvidenceCreated        orchestration.verify
FindingCreated         orchestration.verify    (VIOLATION only)
FindingVerified        orchestration.verify    (VIOLATION only)
GraphUpdated           orchestration.update
AgentFinished          orchestration.replan    (once per retired agent)
RunFinished            app.bootstrap
```

`HypothesisCreated`, `AgentFinished`, `FindingCreated` and
`FindingVerified` were reserved in `EventType` but listed as "not
emitted yet — reserved for Agent Team's planner work" in
`docs/contracts/events.md`. They are emitted now. **That list in
`docs/contracts/events.md` and the one in
`docs/architecture/execution-flow.md` are PM-owned and still say
otherwise — they need updating.**

A blocked action (DENY / REQUIRE_APPROVAL) still emits `GraphUpdated`:
nothing ran, but the graph records that the action was not taken and
why. See [orchestration.md](orchestration.md).

## Swapping the backend

Implement `publish`/`subscribe` against Redis/NATS/Kafka in this package
and wire it in `redteam.app.bootstrap`. Nothing else changes —
`EventPublisher` only depends on the `EventBus` Protocol. Note that
`history()`/`correlated()` are conveniences of the in-memory backend;
code that needs a durable trail should read the Evidence Store, not the
bus.
