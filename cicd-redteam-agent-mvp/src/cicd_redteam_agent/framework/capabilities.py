from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass, field

from hackingBuddyGPT.capability import Capability

from cicd_redteam_agent.domain.github_actions_parser import stable_id
from cicd_redteam_agent.domain.models import AnalysisProposal, AnalysisState, Evidence
from cicd_redteam_agent.domain.validator import apply_analysis_proposal
from cicd_redteam_agent.security.redaction import redact
from cicd_redteam_agent.security.repository_boundary import RepositoryBoundary


@dataclass
class CapabilityContext:
    state: AnalysisState
    boundary: RepositoryBoundary
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
        relations = [
            item.model_dump(mode="json")
            for item in self.context.state.relations
            if item.source_id.startswith(workflow_id)
        ]
        secrets = [
            item.model_dump(mode="json")
            for item in self.context.state.secret_references
            if item.workflow_id == workflow_id
        ]
        return json.dumps(
            {
                "workflow": workflow.model_dump(mode="json", by_alias=True),
                "relations": relations,
                "secret_references": secrets,
            }
        )


class InspectRepositoryFile(Capability):
    def __init__(self, context: CapabilityContext) -> None:
        self.context = context

    def describe(self) -> str:
        return "Read a redacted 1-200 line slice of an in-repository UTF-8 file."

    async def __call__(self, path: str, start_line: int, end_line: int) -> str:
        self.context.record_call(
            self.get_name(), {"path": path, "start_line": start_line, "end_line": end_line}
        )
        excerpt = self.context.boundary.read_slice(path, start_line, end_line)
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
        matches = self.context.boundary.search_literal(query, include_globs, max_results)
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
            "Submit a JSON-encoded AnalysisProposal. It may reference only existing Evidence IDs; "
            "static attack paths cannot be CONFIRMED."
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
        GetWorkflowFacts(context),
        InspectRepositoryFile(context),
        SearchRepository(context),
        SubmitAnalysis(context),
    ]
    return {capability.get_name(): capability for capability in capabilities}
