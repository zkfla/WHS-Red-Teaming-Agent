"""Agent-Team event names that are not part of the frozen Core vocabulary."""

CANDIDATE_PRUNED = "CandidatePruned"
AGENT_PROVIDER_FAILED = "AgentProviderFailed"
REPORT_PROJECTED = "ReportProjected"
#: An agent activated (a provider offered at least one draft step -- the
#: preconditions in its playbook genuinely matched something in the
#: graph) but every draft was dropped during candidate compilation
#: (no matching/healthy tool, or a required tool-schema/graph-context
#: field could not be resolved). Distinct from the ordinary "nothing to
#: do" case (zero drafts), which is not surfaced at all -- this is
#: specifically "there was something to try, and it still produced
#: nothing executable" (section 10 of the spec this implements).
AGENT_NO_EXECUTABLE_CANDIDATES = "AgentNoExecutableCandidates"
