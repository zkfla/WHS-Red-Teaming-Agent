from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass, field

from hackingBuddyGPT.capability import Capability

from cicd_redteam_agent.domain.github_actions_parser import stable_id
from cicd_redteam_agent.domain.models import AnalysisMode, AnalysisProposal, AnalysisState, Evidence
from cicd_redteam_agent.domain.validator import apply_analysis_proposal
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
    submitted: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

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
            "relations. Filters are optional. These are facts, not vulnerability conclusions."
        )

    async def __call__(
        self, category: str | None = None, verdict: str | None = None
    ) -> str:
        arguments = {"category": category, "verdict": verdict}
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
            return json.dumps(
                {"error": "invalid_category", "allowed": sorted(allowed_categories)}
            )
        if verdict is not None and verdict not in allowed_verdicts:
            return json.dumps(
                {"error": "invalid_verdict", "allowed": sorted(allowed_verdicts)}
            )
        facts = [
            item
            for item in self.context.state.relations
            if item.category is not None
            and (category is None or item.category == category)
            and (verdict is None or item.verdict.value == verdict)
        ]
        counts: dict[str, int] = {}
        for item in facts:
            key = item.category or "uncategorized"
            counts[key] = counts.get(key, 0) + 1
        return json.dumps(
            {
                "facts": [item.model_dump(mode="json") for item in facts],
                "counts": counts,
                "guidance": (
                    "Compose only returned relations into paths and copy each relation_id exactly. "
                    "UNKNOWN relations require a CANDIDATE or REJECTED path and their "
                    "missing_evidence must be preserved."
                ),
            }
        )


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
            "Submit a JSON-encoded AnalysisProposal. Path edges must reference an extracted "
            "relation_id and existing Evidence IDs; static attack paths cannot be CONFIRMED. "
            "The proposal JSON top-level keys are exactly "
            "schema_version, findings, paths, and identities; use paths, not attack_paths."
        )

    async def __call__(self, proposal_json: str) -> str:
        self.context.record_call(self.get_name(), {"proposal_json": proposal_json})
        try:
            proposal = AnalysisProposal.model_validate_json(proposal_json)
        except Exception as exc:
            return json.dumps({"accepted": False, "errors": [f"invalid_proposal: {exc}"]})
        async with self.context.lock:
            if self.context.submitted:
                return json.dumps({"accepted": False, "errors": ["analysis_already_submitted"]})
            result = apply_analysis_proposal(self.context.state, proposal)
            if result.accepted:
                self.context.submitted = True
        return result.model_dump_json()


def build_capabilities(context: CapabilityContext) -> dict[str, Capability]:
    capabilities = [
        ListWorkflows(context),
        ListSecurityFacts(context),
        GetWorkflowFacts(context),
        InspectRepositoryFile(context),
        SearchRepository(context),
        SubmitAnalysis(context),
    ]
    return {capability.get_name(): capability for capability in capabilities}
