# Architecture

Redteaming is an evidence-first authorized assessment controller. The LLM is
an advisory planner and hypothesis prioritizer; the local control plane owns
authorization, execution, verification, and Finding publication.

## Runtime flow

```text
operator request
    -> Scope intake and PlanPolicy
    -> Objective Graph
    -> Worker: TCP recon / HTTP surface discovery / capability adapter
    -> Asset Graph + Exploration Graph + EvidenceStore
    -> specialist hypothesis agents
    -> HypothesisGate
    -> bounded safe replay verification
    -> FindingPublisher evidence gate
    -> explicit report/evaluation request
```

Every stage emits an `Event`. The CLI renders those events as a live execution
timeline and the report preserves the timeline for review.

## Twin graph model

`AssetGraph` is the normalized assessment surface: hosts, services, endpoints,
and assets imported through adapters. `ExplorationGraph` is the per-engagement
reasoning trail: intents, scenarios, hypotheses, candidates, evidence, and
Findings. Anchor and relation edges connect the two without putting the entire
history into an LLM conversation.

The graph never grants permission. Scope checks run before every network probe
and capability execution; graph nodes only describe what was observed or
planned.
Scope entries are matched independently by host, declared scheme, and port;
implicit HTTP/HTTPS default ports are normalized before comparison.

## Roles and policy boundaries

- Planner: produces a Scope-bound `EngagementPlan` and scenario metadata.
- Worker: performs one bounded recon or surface-discovery operation.
- Hypothesis agents: convert normalized observations into typed candidates.
- Verifier: replays only allowed safe requests or a detector-owned verifier.
- Publisher: requires confirmed status, evidence, reproduction steps, impact,
  and an affected asset before creating a Finding.
- Reporter: runs only after an explicit `/report` or `/generate-report` request.

LLM responses are parsed as advisory JSON and validated by `PlanPolicy` or
`HypothesisGate`. Unknown IDs, mutations, shell commands, targets, and payloads
cannot be introduced through the LLM path.

## Extension points

- Register a detector with `LocalRuntime.register_detector`.
- Pass `detector_names=(...)` to `LocalRuntime.run_http_assessment` or
  `EngagementOrchestrator.execute` to run an explicitly selected specialist
  subset; unknown names are rejected by the registry.
- Provide a detector `verify(...)` method for custom evidence comparison.
- Load operator-authored tool and skill metadata through JSON manifests.
- Implement a `ToolOutputParser` and execute it through `CapabilityAdapter`.
- Add scenario metadata without adding an exploit path.
- Add evaluation cases through the owner-supplied JSON schema.

External output is untrusted: credentials are redacted, output is bounded and
quarantined, and prompt-injection markers are recorded as metadata. HTTP
responses use the same boundary while retaining a separate sanitized analysis
copy for deterministic detectors.

## Recovery and reproducibility

The port checkpoint resumes incomplete probes. The engagement snapshot restores
redacted Evidence, Assets, Findings, graph edges, events, and the last Plan.
Completed objectives for the same Plan and Scope are skipped on resume. Identity
profiles and secret headers are never persisted. Evaluation compares owner
ground truth, validated Finding quality, and stable category/asset signatures
across repeated runs.

Replacing a Scope creates a fresh engagement boundary in the CLI; additive
targets supplied with `/scope-add` remain in the same authorized engagement.
Exact-port recon checkpoint keys include the port so two services on one host
cannot inherit each other's completion state.
