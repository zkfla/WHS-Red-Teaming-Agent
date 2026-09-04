from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass, field

from hackingBuddyGPT.capability import Capability

from cicd_redteam_agent.application.objective import CompiledObjectiveGoal, objective_relation_score
from cicd_redteam_agent.domain.github_actions_parser import stable_id
from cicd_redteam_agent.domain.graph_frontier import (
    DEFAULT_MAX_EXPANSIONS,
    DEFAULT_MAX_FRONTIERS,
    FrontierQuery,
    FrontierSearchResult,
    build_attack_frontiers,
)
from cicd_redteam_agent.domain.investigation_policy import (
    InvestigationPolicy,
    InvestigationTransition,
    decide_investigation_policy,
)
from cicd_redteam_agent.domain.models import (
    AnalysisMode,
    AnalysisProposal,
    AnalysisProposalDraft,
    AnalysisState,
    Evidence,
)
from cicd_redteam_agent.domain.proposal_draft import (
    ProposalDraftError,
    expand_analysis_proposal_draft,
    indexed_relations,
)
from cicd_redteam_agent.domain.security_candidate import (
    commit_candidate_decisions,
    commit_frontier_decisions,
    materialize_candidate_decisions,
)
from cicd_redteam_agent.domain.validator import (
    apply_analysis_proposal,
    novel_claim_contracts,
)
from cicd_redteam_agent.framework.working_memory import wrap_capabilities_with_memory
from cicd_redteam_agent.security.redaction import redact
from cicd_redteam_agent.security.repository_boundary import (
    RepositoryBoundary,
    RepositoryBoundaryError,
)


@dataclass
class CapabilityContext:
    state: AnalysisState
    boundary: RepositoryBoundary
    analysis_mode: AnalysisMode = AnalysisMode.DETERMINISTIC
    objective_goal: CompiledObjectiveGoal | None = None
    submitted: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    frontier_cache: dict[str, FrontierSearchResult] = field(default_factory=dict)
    investigation_policy: InvestigationPolicy | None = None

    def record_call(self, name: str, arguments: object) -> int:
        signature = stable_id("call", name, json.dumps(arguments, sort_keys=True, default=str))
        self.state.repeated_calls[signature] = self.state.repeated_calls.get(signature, 0) + 1
        return self.state.repeated_calls[signature]


class ListWorkflows(Capability):
    def __init__(self, context: CapabilityContext) -> None:
        self.context = context

    def describe(self) -> str:
        return "List workflows that were already parsed. This does not read or execute files."

    async def __call__(self) -> str:
        self.context.record_call(self.get_name(), {})
        payload = [
            {
                "workflow_id": workflow.workflow_id,
                "path": workflow.path,
                "triggers": list(workflow.triggers),
                "jobs": [job.job_id for job in workflow.jobs],
                "parse_warnings": workflow.parse_warnings,
            }
            for workflow in self.context.state.workflows
        ]
        return json.dumps({"workflows": payload})


class ListSecurityFacts(Capability):
    def __init__(self, context: CapabilityContext) -> None:
        self.context = context

    def describe(self) -> str:
        return (
            "List deterministic source, transform, execution, credential, guard and context "
            "relations. Filters are optional. The default compact response uses short relation "
            "refs and omits exact grounding IDs; request include_grounding only for legacy full "
            "proposal compatibility. These are facts, not vulnerability conclusions."
        )

    async def __call__(
        self,
        category: str | None = None,
        verdict: str | None = None,
        offset: int = 0,
        limit: int = 100,
        include_grounding: bool = False,
    ) -> str:
        arguments = {
            "category": category,
            "verdict": verdict,
            "offset": offset,
            "limit": limit,
            "include_grounding": include_grounding,
        }
        self.context.record_call(self.get_name(), arguments)
        allowed_categories = {
            "source",
            "transform",
            "execution",
            "credential",
            "guard",
            "context",
        }
        allowed_verdicts = {"TRUE", "FALSE", "UNKNOWN"}
        if category is not None and category not in allowed_categories:
            return json.dumps({"error": "invalid_category", "allowed": sorted(allowed_categories)})
        if verdict is not None and verdict not in allowed_verdicts:
            return json.dumps({"error": "invalid_verdict", "allowed": sorted(allowed_verdicts)})
        if offset < 0:
            return json.dumps({"error": "invalid_offset", "minimum": 0})
        if limit < 1 or limit > 100:
            return json.dumps({"error": "invalid_limit", "minimum": 1, "maximum": 100})
        resource = (
            f"security-facts:{category or 'all'}:{verdict or 'all'}:{offset}:{limit}:"
            f"{'grounded' if include_grounding else 'compact'}"
        )
        if resource not in self.context.state.inspected_resources:
            self.context.state.inspected_resources.append(resource)
        facts = [
            (reference, item)
            for reference, item in indexed_relations(self.context.state)
            if item.category is not None
            and (category is None or item.category == category)
            and (verdict is None or item.verdict.value == verdict)
        ]
        category_order = {
            "source": 0,
            "transform": 1,
            "execution": 2,
            "credential": 3,
            "guard": 4,
            "context": 5,
        }
        goal = self.context.objective_goal
        if goal is not None and goal.focused:
            facts.sort(
                key=lambda pair: (
                    -objective_relation_score(
                        goal,
                        source_id=pair[1].source_id,
                        relation_type=pair[1].relation_type,
                        target_id=pair[1].target_id,
                        category=pair[1].category,
                    ),
                    category_order.get(pair[1].category or "context", 6),
                    pair[0],
                )
            )
        else:
            facts.sort(key=lambda pair: category_order.get(pair[1].category or "context", 6))
        counts: dict[str, int] = {}
        for _, item in facts:
            key = item.category or "uncategorized"
            counts[key] = counts.get(key, 0) + 1
        page = facts[offset : offset + limit]
        next_offset = offset + len(page)
        return json.dumps(
            {
                "facts": [
                    (
                        {"relation_ref": reference, **item.model_dump(mode="json")}
                        if include_grounding
                        else {
                            "relation_ref": reference,
                            "source_id": item.source_id,
                            "relation_type": item.relation_type,
                            "target_id": item.target_id,
                            "category": item.category,
                            "verdict": item.verdict.value,
                            "evidence_count": len(item.evidence_ids),
                            "missing_evidence": item.missing_evidence,
                        }
                    )
                    for reference, item in page
                ],
                "counts": counts,
                "total": len(facts),
                "offset": offset,
                "limit": limit,
                "next_offset": next_offset if next_offset < len(facts) else None,
                "objective_focus": (
                    self.context.objective_goal.as_dict()
                    if self.context.objective_goal is not None
                    else None
                ),
                "guidance": (
                    "When objective_focus.focused is true, rows are relevance-ranked only as an "
                    "investigation hint; ranking never changes relation verdicts or validator "
                    "semantics. "
                    "Compose only returned relations into paths. Use the short relation_ref values "
                    "for compact proposal and working-memory submissions so the runtime can "
                    "restore exact relation, node and Evidence fields. "
                    "UNKNOWN relations require a CANDIDATE or REJECTED path and their "
                    "missing_evidence must be preserved. FALSE relations require a REJECTED path. "
                    "Do not report isolated authority, dependency, guard, or context facts as an "
                    "attack without an ordered source-to-execution-to-sensitive-sink chain. "
                    "Follow next_offset until null. Do not repeat this capability with narrower "
                    "filters after an unfiltered response reached next_offset null."
                ),
            }
        )


class ListAttackFrontiers(Capability):
    def __init__(self, context: CapabilityContext) -> None:
        self.context = context

    def describe(self) -> str:
        return (
            "List bounded, context-sensitive source-to-sink attack-path candidates derived from "
            "deterministic Relations. Each candidate includes continuous relation_refs, relevant "
            "guards, unresolved assumptions, execution context, status ceiling, and whether it is "
            "proposal-ready. Frontiers are investigation aids, not Findings."
        )

    async def __call__(
        self,
        offset: int = 0,
        limit: int = 10,
        include_blocked: bool = True,
    ) -> str:
        arguments = {
            "offset": offset,
            "limit": limit,
            "include_blocked": include_blocked,
        }
        self.context.record_call(self.get_name(), arguments)
        if offset < 0:
            return json.dumps({"error": "invalid_offset", "minimum": 0})
        if limit < 1 or limit > 20:
            return json.dumps({"error": "invalid_limit", "minimum": 1, "maximum": 20})

        goal = self.context.objective_goal
        sink_hints = goal.sink_hints if goal else ()
        if "privileged_execution" in sink_hints:
            sink_hints = tuple(
                dict.fromkeys(
                    (
                        *(item for item in sink_hints if item != "privileged_execution"),
                        "github_token_write",
                        "oidc_token",
                        "cloud_identity",
                        "secret_exposure",
                    )
                )
            )
        if "cloud_identity" in sink_hints:
            sink_hints = tuple(item for item in sink_hints if item != "oidc_token")
        query = FrontierQuery(
            source_hints=goal.source_hints if goal else (),
            mechanism_hints=goal.mechanism_hints if goal else (),
            sink_hints=sink_hints,
            max_frontiers=DEFAULT_MAX_FRONTIERS,
            max_expansions=DEFAULT_MAX_EXPANSIONS,
        )
        cache_key = json.dumps(
            {
                "source_hints": query.source_hints,
                "mechanism_hints": query.mechanism_hints,
                "sink_hints": query.sink_hints,
            },
            sort_keys=True,
        )
        result = self.context.frontier_cache.get(cache_key)
        if result is None:
            result = build_attack_frontiers(self.context.state, query)
            self.context.frontier_cache[cache_key] = result

        frontiers = [
            item
            for item in result.frontiers
            if include_blocked or item.disposition.value != "BLOCKED"
        ]
        counts: dict[str, int] = {}
        for item in frontiers:
            counts[item.disposition.value] = counts.get(item.disposition.value, 0) + 1
        resource = f"attack-frontiers:{offset}:{limit}:{include_blocked}:{cache_key}"
        if resource not in self.context.state.inspected_resources:
            self.context.state.inspected_resources.append(resource)

        payload = result.as_dict()
        policy = self.context.investigation_policy or decide_investigation_policy(
            result,
            self.context.state,
            focused=bool(goal and goal.focused),
        )
        selected_candidates = [
            entry.as_dict(compact=True)["candidate"]
            for entry in policy.portfolio
            if entry.candidate is not None
        ]
        root_cause_keys = {candidate.root_cause_key for candidate in self.context.state.candidates}
        terminal_root_cause_keys = {
            root_cause_key
            for root_cause_key in root_cause_keys
            if all(
                candidate.disposition.value
                in {"ACCEPTED", "REJECTED", "BLOCKED_EXTERNAL", "MERGED"}
                for candidate in self.context.state.candidates
                if candidate.root_cause_key == root_cause_key
            )
        }
        selected_frontier_ids = {
            entry.frontier.frontier_id
            for entry in policy.portfolio
            if entry.frontier is not None
        }
        if self.context.state.candidates and not (goal and goal.focused):
            # In broad candidate-driven analysis, raw repository-wide top frontiers are diagnostics,
            # not peer decision targets.  Returning stale top rows here caused the model to submit
            # old frontier IDs after the candidate portfolio rotated.
            decision_frontiers = [
                item for item in result.frontiers if item.frontier_id in selected_frontier_ids
            ]
            visible_frontiers = decision_frontiers
        else:
            visible_frontiers = frontiers
        visible_page = visible_frontiers[offset : offset + limit]
        visible_next_offset = offset + len(visible_page)

        payload.update(
            {
                "frontiers": [item.as_dict() for item in visible_page],
                "total": len(visible_frontiers),
                "counts": counts,
                "frontier_inventory": {
                    "discovered_total": len(result.frontiers),
                    "visible_decision_frontiers": len(visible_frontiers),
                    "terminal_or_diagnostic_frontiers_hidden": max(
                        0, len(result.frontiers) - len(visible_frontiers)
                    ),
                },
                "offset": offset,
                "limit": limit,
                "next_offset": (
                    visible_next_offset if visible_next_offset < len(visible_frontiers) else None
                ),
                "objective_focus": goal.as_dict() if goal else None,
                "investigation_policy": policy.as_dict(compact=True),
                # Candidate details are already represented by the current deterministic
                # portfolio.  Return them only on the first Frontier page; later pages are graph
                # pagination, not a reason to resend the repository-wide Candidate universe.
                "security_candidates": selected_candidates if offset == 0 else [],
                "security_candidates_included": offset == 0,
                "candidate_inventory": {
                    "total_candidates": len(self.context.state.candidates),
                    "root_cause_instances": len(root_cause_keys),
                    "terminal_root_cause_instances": len(terminal_root_cause_keys),
                    "remaining_root_cause_instances": (
                        len(root_cause_keys) - len(terminal_root_cause_keys)
                    ),
                    "selected_candidate_representatives": len(selected_candidates),
                },
                "novel_claim_contracts": novel_claim_contracts(),
                "guidance": (
                    "Use canonical security_candidates as the primary semantic proposal "
                    "candidates. "
                    "Submit terminal candidate_decisions for every selected candidate in a broad "
                    "objective. The `frontiers` array contains only current-batch frontier "
                    "decision "
                    "targets when canonical candidates exist; repository-wide frontier counts are "
                    "diagnostic inventory only. "
                    "PARTIAL frontiers are diagnostics only and must never be submitted. BLOCKED "
                    "frontiers are terminal for that execution context and should not be "
                    "repeatedly investigated. UNKNOWN reasons marked EXTERNAL_REQUIRED cannot be "
                    "resolved by additional repository reads. Objective ranking changes neither "
                    "Relations nor "
                    "Validator semantics. A truncated response never proves that no path exists."
                ),
            }
        )
        return json.dumps(payload)


class GetWorkflowFacts(Capability):
    def __init__(self, context: CapabilityContext) -> None:
        self.context = context

    def describe(self) -> str:
        return "Return parsed facts and existing Evidence IDs for one workflow ID."

    async def __call__(self, workflow_id: str) -> str:
        self.context.record_call(self.get_name(), {"workflow_id": workflow_id})
        workflow = next(
            (item for item in self.context.state.workflows if item.workflow_id == workflow_id), None
        )
        if workflow is None:
            return json.dumps({"error": "unknown_workflow_id", "workflow_id": workflow_id})
        member_ids = {workflow.workflow_id}
        for job in workflow.jobs:
            member_ids.add(job.job_id)
            member_ids.update(step.step_id for step in job.steps)
        relations = [
            item.model_dump(mode="json")
            for item in self.context.state.relations
            if item.source_id in member_ids or item.target_id in member_ids
        ]
        secrets = [
            item.model_dump(mode="json")
            for item in self.context.state.secret_references
            if item.workflow_id == workflow_id
        ]
        payload = {
            "workflow": workflow.model_dump(mode="json", by_alias=True),
            "relations": relations,
            "secret_references": secrets,
            "assessments": [
                item.model_dump(mode="json")
                for item in self.context.state.assessments
                if item.subject_id in member_ids
            ],
            "identities": [
                item.model_dump(mode="json")
                for item in self.context.state.identities
                if item.workflow_id == workflow_id
            ],
            "security_candidates": [
                {
                        "candidate_id": item.candidate_id,
                        "candidate_kind": item.candidate_kind.value,
                        "root_cause_family": item.root_cause_family,
                        "root_cause_key": item.root_cause_key,
                    "status": item.status.value,
                    "proposal_status_ceiling": item.proposal_status_ceiling.value,
                    "disposition": item.disposition.value,
                        "supporting_relation_ids": item.supporting_relation_ids,
                        "evidence_requirements": [
                            requirement.model_dump(mode="json")
                            for requirement in item.evidence_requirements
                        ],
                        "missing_evidence": item.missing_evidence,
                    "claim_contract": item.claim_contract.model_dump(mode="json"),
                }
                for item in self.context.state.candidates
                if item.workflow_id == workflow_id
            ],
        }
        if self.context.analysis_mode is AnalysisMode.DETERMINISTIC:
            payload["findings"] = [
                item.model_dump(mode="json")
                for item in self.context.state.findings
                if any(
                    path.path_id == stable_id("path", item.finding_id)
                    and bool(member_ids & set(path.nodes))
                    for path in self.context.state.paths
                )
            ]
            payload["attack_paths"] = [
                item.model_dump(mode="json")
                for item in self.context.state.paths
                if member_ids & set(item.nodes)
            ]
        return json.dumps(payload)


class InspectRepositoryFile(Capability):
    def __init__(self, context: CapabilityContext) -> None:
        self.context = context

    def describe(self) -> str:
        return "Read a redacted 1-200 line slice of an in-repository UTF-8 file."

    async def __call__(self, path: str, start_line: int, end_line: int) -> str:
        self.context.record_call(
            self.get_name(), {"path": path, "start_line": start_line, "end_line": end_line}
        )
        try:
            excerpt = self.context.boundary.read_slice(path, start_line, end_line)
        except RepositoryBoundaryError as exc:
            return json.dumps({"error": "repository_boundary_error", "detail": str(exc)})
        evidence_id = stable_id("ev", path, start_line, end_line, excerpt)
        evidence = Evidence(
            evidence_id=evidence_id,
            kind="file_slice",
            source_path=path,
            line_start=start_line,
            line_end=end_line,
            field_path="$",
            redacted_excerpt=excerpt,
            content_hash=hashlib.sha256(excerpt.encode()).hexdigest(),
        )
        async with self.context.lock:
            self.context.state.evidence[evidence_id] = evidence
            if path not in self.context.state.inspected_resources:
                self.context.state.inspected_resources.append(path)
        return json.dumps({"evidence_id": evidence_id, "path": path, "content": excerpt})


class SearchRepository(Capability):
    def __init__(self, context: CapabilityContext) -> None:
        self.context = context

    def describe(self) -> str:
        return "Literal-only repository search. Returns at most 50 redacted matches."

    async def __call__(
        self, query: str, include_globs: list[str] | None = None, max_results: int = 50
    ) -> str:
        self.context.record_call(
            self.get_name(),
            {"query": query, "include_globs": include_globs, "max_results": max_results},
        )
        try:
            matches = self.context.boundary.search_literal(query, include_globs, max_results)
        except RepositoryBoundaryError as exc:
            return json.dumps({"error": "repository_boundary_error", "detail": str(exc)})
        async with self.context.lock:
            for match in matches:
                excerpt = str(match["excerpt"])
                evidence_id = stable_id("ev", match["path"], match["line"], excerpt)
                self.context.state.evidence[evidence_id] = Evidence(
                    evidence_id=evidence_id,
                    kind="search_match",
                    source_path=str(match["path"]),
                    line_start=int(match["line"]),
                    line_end=int(match["line"]),
                    field_path="$",
                    redacted_excerpt=redact(excerpt),
                    content_hash=str(match["content_hash"]),
                )
                match["evidence_id"] = evidence_id
        return json.dumps({"matches": matches})


class SubmitAnalysis(Capability):
    def __init__(self, context: CapabilityContext) -> None:
        self.context = context

    def describe(self) -> str:
        return (
            "Submit JSON as either a compact AnalysisProposalDraft (preferred) or a full "
            "AnalysisProposal. Candidate-backed compact top-level keys are schema_version and "
            "candidate_decisions; legacy compact keys are schema_version, finding_drafts, and "
            "path_drafts. Each path draft has path_id and ordered relation_refs such as R0001; "
            "the runtime derives exact nodes, edges, status, Evidence and missing evidence. "
            "Each finding draft has finding_id, rule_id, title, path_id, confidence, summary. "
            "For a full proposal, path edges must reference an extracted "
            "relation_id and existing Evidence IDs; static attack paths cannot be CONFIRMED. "
            "The proposal JSON top-level keys are exactly "
            "schema_version, findings, paths, and identities; use paths, not attack_paths. "
            "Finding fields are finding_id, rule_id, title, status, evidence_ids, "
            "missing_evidence, confidence, summary. AttackPath fields are path_id, nodes, "
            "edges, status, evidence_ids, missing_evidence; do not add finding_id or "
            "finding_title. AttackEdge fields are relation_id, source_id, relation_type, "
            "target_id, evidence_ids. Each edge must connect nodes[i] to nodes[i+1], so "
            "len(edges) must equal len(nodes)-1. Include only one continuous chain, not "
            "branching facts, and copy the union of all edge Evidence IDs into the path's "
            "evidence_ids. Confidence is LOW, MEDIUM, or HIGH. Candidate decisions use "
            "candidate_id, decision (ACCEPTED, REJECTED, BLOCKED_EXTERNAL, or MERGED), "
            "optional selected_path_variant, confidence, and optional analyst_note. The runtime "
            "supplies canonical rule, title, summary, status ceiling, Evidence and missing "
            "evidence from the candidate contract. Frontier decisions use frontier_id, decision, "
            "optional confidence, and optional analyst_note. In a mixed candidate/frontier batch, "
            "an ACCEPTED frontier must be accompanied by exactly one path_draft containing an "
            "exact allowed relation_refs path for that selected frontier. This is the only "
            "path_draft allowed alongside candidate_decisions; do not add finding_drafts to a "
            "candidate-backed mixed batch. REJECTED, BLOCKED_EXTERNAL, and MERGED frontier "
            "decisions need no path_draft."
        )

    async def __call__(self, proposal_json: str) -> str:
        self.context.record_call(self.get_name(), {"proposal_json": proposal_json})
        try:
            raw_proposal = json.loads(proposal_json)
            if not isinstance(raw_proposal, dict):
                raise ValueError("proposal must be a JSON object")
            candidate_decisions = ()
            frontier_decisions = ()
            if (
                "candidate_decisions" in raw_proposal
                or "frontier_decisions" in raw_proposal
                or "finding_drafts" in raw_proposal
                or "path_drafts" in raw_proposal
            ):
                draft = AnalysisProposalDraft.model_validate(raw_proposal)
                frontier_decisions = tuple(draft.frontier_decisions)
                if draft.candidate_decisions:
                    if draft.finding_drafts:
                        raise ValueError(
                            "candidate_decisions cannot be mixed with finding_drafts"
                        )
                    proposal, candidate_decisions = materialize_candidate_decisions(
                        self.context.state,
                        draft.candidate_decisions,
                    )
                    if draft.path_drafts:
                        frontier_draft = draft.model_copy(
                            update={
                                "candidate_decisions": [],
                                "frontier_decisions": [],
                            }
                        )
                        frontier_proposal = expand_analysis_proposal_draft(
                            self.context.state,
                            frontier_draft,
                        )
                        proposal.paths.extend(frontier_proposal.paths)
                else:
                    proposal = expand_analysis_proposal_draft(self.context.state, draft)
            else:
                proposal = AnalysisProposal.model_validate(raw_proposal)
        except (ProposalDraftError, ValueError) as exc:
            return json.dumps({"accepted": False, "errors": [f"invalid_proposal: {exc}"]})
        async with self.context.lock:
            if self.context.submitted:
                return json.dumps({"accepted": False, "errors": ["analysis_already_submitted"]})
            policy = self.context.investigation_policy
            if policy is not None:
                policy_errors = self._policy_errors(
                    proposal,
                    policy,
                    candidate_decisions=candidate_decisions,
                    frontier_decisions=frontier_decisions,
                )
                if policy_errors:
                    return json.dumps({"accepted": False, "errors": policy_errors})
            result = apply_analysis_proposal(self.context.state, proposal)
            if result.accepted:
                if candidate_decisions:
                    commit_candidate_decisions(self.context.state, candidate_decisions)
                if frontier_decisions:
                    commit_frontier_decisions(self.context.state, frontier_decisions)
                batch_committed = bool(
                    policy is not None
                    and policy.transition is InvestigationTransition.INVESTIGATE
                    and policy.decision_submission_allowed
                    and not policy.focused
                    and (candidate_decisions or frontier_decisions)
                )
                self.context.submitted = not batch_committed
                if batch_committed:
                    return json.dumps(
                        {
                            "accepted": True,
                            "errors": [],
                            "batch_committed": True,
                            "analysis_complete": False,
                        }
                    )
        return result.model_dump_json()

    def _policy_errors(
        self,
        proposal: AnalysisProposal,
        policy: InvestigationPolicy,
        *,
        candidate_decisions=(),
        frontier_decisions=(),
    ) -> list[str]:
        if (
            policy.transition is not InvestigationTransition.SUBMIT
            and not policy.decision_submission_allowed
        ):
            return [f"submission_transition_not_ready:{policy.transition.value}"]
        relation_by_ref = dict(indexed_relations(self.context.state))
        selected_candidates = {
            entry.candidate.candidate_id
            for entry in policy.portfolio
            if entry.candidate is not None
        }
        if selected_candidates:
            decision_ids = {decision.candidate_id for decision in candidate_decisions}
            if not candidate_decisions:
                return ["candidate_decisions_required_for_selected_portfolio"]
            outside = decision_ids - selected_candidates
            if outside:
                return [
                    "candidate_decision_outside_selected_portfolio:"
                    + ",".join(sorted(outside))
                ]
            missing = selected_candidates - decision_ids
            if missing:
                return [
                    "selected_candidate_without_terminal_disposition:"
                    + ",".join(sorted(missing))
                ]
        selected_frontiers = {
            entry.frontier.frontier_id
            for entry in policy.portfolio
            if entry.frontier is not None
        }
        # Keep the pre-candidate frontier-only contract compatible for states that contain no
        # semantic candidates. Candidate-aware portfolios must explicitly account for every
        # selected graph frontier so broad coverage cannot be silently skipped.
        if selected_frontiers and (selected_candidates or self.context.state.candidates):
            frontier_decision_ids = {
                decision.frontier_id for decision in frontier_decisions
            }
            if not frontier_decisions:
                return ["frontier_decisions_required_for_selected_portfolio"]
            outside = frontier_decision_ids - selected_frontiers
            if outside:
                return [
                    "frontier_decision_outside_selected_portfolio:"
                    + ",".join(sorted(outside))
                ]
            missing = selected_frontiers - frontier_decision_ids
            if missing:
                return [
                    "selected_frontier_without_terminal_disposition:"
                    + ",".join(sorted(missing))
                ]
            accepted_frontiers = {
                decision.frontier_id
                for decision in frontier_decisions
                if decision.decision.value == "ACCEPTED"
            }
            frontier_relation_paths = {
                entry.frontier.frontier_id: tuple(
                    relation.relation_id
                    for reference in entry.frontier.relation_refs
                    if (relation := relation_by_ref.get(reference)) is not None
                )
                for entry in policy.portfolio
                if entry.frontier is not None
            }
            submitted_path_keys = {
                tuple(edge.relation_id for edge in path.edges)
                for path in proposal.paths
            }
            missing_frontier_paths = {
                frontier_id
                for frontier_id in accepted_frontiers
                if frontier_relation_paths.get(frontier_id) not in submitted_path_keys
            }
            if missing_frontier_paths:
                return [
                    "accepted_frontier_without_grounded_path:"
                    + ",".join(sorted(missing_frontier_paths))
                ]
        if not proposal.paths:
            if candidate_decisions and all(
                decision.decision.value != "ACCEPTED"
                for decision in candidate_decisions
            ):
                return []
            return [] if policy.empty_submission_allowed else ["empty_submission_not_allowed"]

        allowed = {
            tuple(
                relation.relation_id
                for reference in references
                if (relation := relation_by_ref.get(reference)) is not None
            )
            for references in policy.allowed_relation_ref_paths
        }
        selected_candidate_paths = {
            (
                candidate.candidate_id,
                tuple(edge.relation_id for edge in variant.edges),
            )
            for entry in policy.portfolio
            if entry.candidate is not None
            for candidate in (entry.candidate,)
            for variant in candidate.path_variants
        }
        rejected = [
            path.path_id
            for path in proposal.paths
            if (
                path.candidate_id,
                tuple(edge.relation_id for edge in path.edges),
            ) not in selected_candidate_paths
            and tuple(edge.relation_id for edge in path.edges) not in allowed
        ]
        if rejected:
            return [f"proposal_path_outside_selected_portfolio:{','.join(sorted(rejected))}"]
        return []


def build_capabilities(context: CapabilityContext) -> dict[str, Capability]:
    capabilities = [
        ListWorkflows(context),
        ListAttackFrontiers(context),
        ListSecurityFacts(context),
        GetWorkflowFacts(context),
        InspectRepositoryFile(context),
        SearchRepository(context),
        SubmitAnalysis(context),
    ]
    memory_aware = wrap_capabilities_with_memory(
        capabilities,
        context.state,
        context.lock,
    )
    return {capability.get_name(): capability for capability in memory_aware}
