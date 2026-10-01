"""Agent-Team event names that are not part of the frozen Core vocabulary."""

CANDIDATE_PRUNED = "CandidatePruned"
#: A provider's draft did not compile; payload carries the reason and field.
CANDIDATE_REJECTED = "CandidateRejected"
CANDIDATE_GENERATED = "CandidateGenerated"
AGENT_PROVIDER_FAILED = "AgentProviderFailed"
REPORT_PROJECTED = "ReportProjected"
OBSERVATION_RECORDED = "ObservationRecorded"
GRAPH_MUTATION_APPLIED = "GraphMutationApplied"
HYPOTHESIS_RESOLVED = "HypothesisResolved"
RESULT_MAPPER_FAILED = "ResultMapperFailed"
REPLAN_TRIGGERED = "ReplanTriggered"
AUTHENTICATION_CHECK_DEFERRED = "AuthenticationCheckDeferred"
AUTHENTICATION_CHECKED = "AuthenticationChecked"
#: Bounded summary of the exact context made available to the planner. This
#: exposes inputs and state, never hidden chain-of-thought.
AGENT_CONTEXT_PREPARED = "AgentContextPrepared"
#: Public result of one LLM turn: concise response/assumptions/decision,
#: never private chain-of-thought. Its proposed actions still go through the
#: compiler and Control Plane.
AGENT_TURN_DECIDED = "AgentTurnDecided"
#: Concise, inspectable decision summary after candidate selection.
AGENT_DECISION_RECORDED = "AgentDecisionRecorded"
#: Executable catalog binding selected after planning and before Control.
MCP_TOOL_SELECTED = "MCPToolSelected"
#: An agent activated (a provider offered at least one draft step -- the
#: preconditions in its playbook genuinely matched something in the
#: graph) but every draft was dropped during candidate compilation
#: (no matching/healthy tool, or a required tool-schema/graph-context
#: field could not be resolved). Distinct from the ordinary "nothing to
#: do" case (zero drafts), which is not surfaced at all -- this is
#: specifically "there was something to try, and it still produced
#: nothing executable" (section 10 of the spec this implements).
AGENT_NO_EXECUTABLE_CANDIDATES = "AgentNoExecutableCandidates"
