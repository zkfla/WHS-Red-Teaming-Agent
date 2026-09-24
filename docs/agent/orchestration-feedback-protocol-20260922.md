# ORCH-AGENT-FEEDBACK-20260921 Protocol

## Baseline and ownership

- Baseline: `dev-orchestration` at `64ecfc6b3779`.
- Modify only Agent Team orchestration, shared attack graph, event/CLI integration,
  tests, and `docs/agent` documentation.
- Do not modify Capability, Core contracts, Control Plane, or MCP servers.

## Re-audit result

Already solved:

- Graph-driven candidate regeneration, catalog binding, stable candidate identity,
  bounded LLM reranking, relation-aware reactivation, and ResultMapperRegistry.
- Observation verdict downgrade and evidence/confirmed-hypothesis/duplicate Finding
  safeguards.

Still missing:

- Initial Web observations do not create a reusable validation hypothesis.
- Authentication follow-ups create a second hypothesis and are labelled observation.
- Finding promotion does not enforce the actual step assessment kind.
- Prior-terminal candidates are silently filtered; no-executable summaries omit
  provider/drop diagnostics.
- Candidate/action/finding events do not consistently expose all safe linking ids.
- Analysis and reporting are runtime services/projectors but still look like dormant
  scheduler agents without a concise architectural statement.

External dependencies:

- Redirect Location, WWW-Authenticate scheme/realm, and detailed Set-Cookie metadata
  are not guaranteed by the current ToolResult contract. No value will be inferred.
- There is no explicit non-authentication Web security policy input from which a safe
  response-differential or header-posture violation can be derived.

## Implementation protocol

1. Add an Agent-internal existing-hypothesis reference to PlanStep and propagate it
   through candidate identity, WorkItem scheduling, ActionProposal, Evidence, and
   Finding without changing Core contracts.
2. Make the initial endpoint request state a non-vulnerability hypothesis: access
   behavior may require explicit-policy validation. Observation resolves it to
   inconclusive, which remains selectable by the Web provider.
3. For anonymous 401 plus an operator-registered `expected_access=deny` session,
   generate one catalog-backed `security_check` candidate that reuses that exact
   hypothesis. Keep allow/unspecified comparisons observation-only.
4. Resolve the reused hypothesis to confirmed on deterministic allow/2xx and rejected
   on deterministic deny; retain inconclusive for unavailable or uninterpretable data.
5. Require `assessment_kind=security_check` inside FindingPromoter and pass the real
   PlanStep value from VERIFY. Preserve all existing gates.
6. Emit bounded structured diagnostics for terminal prior execution and include
   compiler, provider, and prerequisite summaries in AgentNoExecutableCandidates.
7. Add safe candidate/hypothesis/action ids to lifecycle event payloads while
   preserving run/action correlation and direct causation.
8. Document AnalysisVerifier as the verification service and ReportingAgent as the
   terminal projector; do not turn either into schedulable tool agents.

## Verification protocol

- Targeted tests: Web follow-ups, authentication integration, Finding promotion,
  candidate diagnostics, event identity, CLI rendering, and convergence/no-loop.
- Regression tests: dynamic candidates, hybrid planner, recon provider, Web analysis,
  result mappers, agent loop, and recon bootstrap.
- Run the full suite, Ruff, and mypy when configured.
- Audit `git diff` to prove zero Capability/Core/Control changes.
- Record exact commands, counts, failures, contract gaps, and final commit in
  `docs/agent/orchestration-implementation-result.md`.
