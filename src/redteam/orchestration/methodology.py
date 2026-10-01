"""Reference metadata/data only. No Capability imports, file IO or direct tool calls."""

from __future__ import annotations

import hashlib
from pathlib import PurePosixPath
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from redteam.core.contracts.action import ActionProposal
from redteam.core.contracts.evidence import EvidenceRecord
from redteam.core.contracts.target import AssetTarget
from redteam.core.contracts.tool import ToolResult
from redteam.core.ids import AgentId, AssetId, GraphNodeId, ToolId
from redteam.orchestration.agents.base import AgentSpec, PlanStep
from redteam.orchestration.agents.definition import AgentDefinition, ContextProfile
from redteam.orchestration.agents.providers import CandidateBatch, GraphCandidateContext
from redteam.orchestration.candidates.models import CandidateDraft

REFERENCE_AGENT_ID = AgentId("methodology-agent")
REFERENCE_PROVIDER_ID = "reference:methodology"
_DIGEST = r"^[a-f0-9]{64}$"
_ID = r"^[a-z][a-z0-9-]{0,63}$"


class ReferenceEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    reference_id: str = Field(pattern=_ID)
    path: str = Field(max_length=512)
    sha256: str = Field(pattern=_DIGEST)
    title: str = Field(min_length=1, max_length=200)
    domains: tuple[
        Literal["general", "web", "api", "recon", "mobile", "cloud_identity", "ai_llm", "vdb"], ...
    ]
    source_id: str = Field(pattern=_ID)
    source_commit: str = Field(pattern=r"^[a-f0-9]{40}$")
    review_caveat: str = Field(min_length=1, max_length=500)

    @field_validator("path")
    @classmethod
    def valid_path(cls, value: str) -> str:
        path = PurePosixPath(value)
        if (
            path.is_absolute()
            or path.as_posix() != value
            or any(p in {".", ".."} for p in path.parts)
            or any(c in value for c in "\\:\x00\r\n")
            or not value.endswith(".md")
        ):
            raise ValueError("invalid reference relative path")
        return value


class ReferenceChunk(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    reference_id: str
    asset_ref: str
    path: str
    sha256: str = Field(pattern=_DIGEST)
    size_bytes: int = Field(ge=0, le=65536)
    offset_chars: int = Field(ge=0, le=65536)
    content: str = Field(max_length=16384)
    next_offset_chars: Annotated[int, Field(ge=1, le=65536)] | None
    truncated: bool


class ReferenceLibrary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    asset_ref: str
    engagement_id: str
    run_id: str
    entries: tuple[ReferenceEntry, ...] = Field(min_length=1, max_length=3)
    read_chars: int = Field(default=8192, ge=256, le=16384)
    max_context_chars: int = Field(default=12000, ge=256, le=24000)

    def check_context(self, engagement_id: str, run_id: str) -> None:
        if (str(engagement_id), str(run_id)) != (self.engagement_id, self.run_id):
            raise ValueError("methodology_context_mismatch")

    def capture(self, proposal: ActionProposal, result: ToolResult) -> dict:
        """Only inspect explicitly selected file reads. Return no unrelated content."""
        entry = next((e for e in self.entries if e.path == proposal.arguments.get("path")), None)
        if entry is None or (proposal.capability, proposal.operation) != ("analysis", "file_read"):
            return {}
        rejected = {"methodology_rejected": entry.reference_id}
        if (
            str(proposal.agent_id) != REFERENCE_AGENT_ID
            or str(proposal.engagement_id) != self.engagement_id
            or str(proposal.run_id) != self.run_id
            or not isinstance(proposal.target, AssetTarget)
            or str(proposal.target.asset_id) != self.asset_ref
            or result.tool_id != ToolId("analysis.read_file")
            or result.action_id != proposal.action_id
            or not result.success
        ):
            return rejected
        try:
            raw = dict(result.output or {})
            if raw.pop("encoding", None) != "utf-8":
                return rejected
            chunk = ReferenceChunk.model_validate({**raw, "reference_id": entry.reference_id})
            if (
                chunk.asset_ref != self.asset_ref
                or chunk.path != entry.path
                or chunk.sha256 != entry.sha256
                or chunk.offset_chars != proposal.arguments.get("offset_chars", 0)
                or len(chunk.content)
                > min(self.read_chars, proposal.arguments.get("max_chars", self.read_chars))
                or len(chunk.content.encode("utf-8")) > chunk.size_bytes
                or (
                    chunk.truncated
                    and (
                        not chunk.content
                        or chunk.next_offset_chars != chunk.offset_chars + len(chunk.content)
                    )
                )
                or (not chunk.truncated and chunk.next_offset_chars is not None)
            ):
                return rejected
            return {"methodology_chunk": chunk.model_dump(mode="json")}
        except (TypeError, ValueError, UnicodeError):
            return rejected

    def inspect(self, records: list[EvidenceRecord], entry: ReferenceEntry) -> tuple[str, int, str]:
        """Reconstruct contiguous pages and independently hash the complete bytes."""
        pages: dict[int, ReferenceChunk] = {}
        for record in records:
            if (str(record.engagement_id), str(record.run_id)) != (self.engagement_id, self.run_id):
                continue
            if (
                record.source_id != "analysis.read_file"
                or record.metadata.get("agent_id") != REFERENCE_AGENT_ID
            ):
                continue
            if record.metadata.get("methodology_rejected") == entry.reference_id:
                return "invalid", 0, ""
            raw = record.metadata.get("methodology_chunk")
            if not isinstance(raw, dict) or raw.get("reference_id") != entry.reference_id:
                continue
            try:
                page = ReferenceChunk.model_validate(raw)
            except ValidationError:
                return "invalid", 0, ""
            if (
                record.metadata.get("success") is not True
                or page.path != entry.path
                or page.asset_ref != self.asset_ref
                or page.sha256 != entry.sha256
                or len(page.content) > self.read_chars
                or (page.offset_chars in pages and pages[page.offset_chars] != page)
            ):
                return "invalid", 0, ""
            pages[page.offset_chars] = page
        if not pages:
            return "pending", 0, ""
        offset, text = 0, ""
        first = pages.get(0)
        if first is None:
            return "invalid", 0, ""
        for _ in range(256):  # minimum page size is 256, document <=64KiB
            page = pages.get(offset)
            if page is None:
                return "pending", offset, ""
            if page.size_bytes != first.size_bytes:
                return "invalid", 0, ""
            text += page.content
            try:
                data = text.encode("utf-8")
            except UnicodeError:
                return "invalid", 0, ""
            if len(data) > 65536 or len(data) > first.size_bytes:
                return "invalid", 0, ""
            if not page.truncated:
                if (
                    page.next_offset_chars is not None
                    or len(data) != first.size_bytes
                    or hashlib.sha256(data).hexdigest() != entry.sha256
                    or any(p > offset for p in pages)
                ):
                    return "invalid", 0, ""
                return "verified", 0, text
            next_offset = offset + len(page.content)
            if not page.content or page.next_offset_chars != next_offset:
                return "invalid", 0, ""
            offset = next_offset
        return "invalid", 0, ""

    def progress(self, records: list[EvidenceRecord]) -> tuple[dict, ...]:
        output = []
        for entry in self.entries:
            status, offset, _text = self.inspect(records, entry)
            output.append(
                {
                    "reference_id": entry.reference_id,
                    "path": entry.path,
                    "status": status,
                    "next_offset_chars": offset,
                }
            )
        return tuple(output)

    def excerpts(self, records: list[EvidenceRecord], capabilities: list[str]) -> tuple[dict, ...]:
        output, remaining = [], self.max_context_chars
        for entry in self.entries:
            if not set(entry.domains).intersection({"general", *capabilities}):
                continue
            status, _offset, text = self.inspect(records, entry)
            if status != "verified" or remaining <= 0:
                continue
            # Share the total prompt budget fairly; full digest was checked above.
            content = text[: min(remaining, self.max_context_chars // len(self.entries))]
            remaining -= len(content)
            output.append(
                {
                    **entry.model_dump(mode="json"),
                    "trust": "untrusted_reference",
                    "execution_permission": False,
                    "content": content,
                    "content_truncated_for_prompt": len(content) < len(text),
                }
            )
        return tuple(output)


class MethodologyCandidateProvider:
    declared_operations = (("analysis", "file_read"),)

    def __init__(self, library: ReferenceLibrary):
        self.library = library

    async def propose_graph(
        self, spec: AgentSpec, context: GraphCandidateContext
    ) -> CandidateBatch:
        target_id = GraphNodeId(f"asset:{self.library.asset_ref}")
        known = {n for ids in context.observation.node_ids_by_type.values() for n in ids}
        if (
            target_id not in known
            or not context.can_execute("analysis", "file_read")
            or (context.task_target_node_ids and target_id not in context.task_target_node_ids)
        ):
            return CandidateBatch(candidates=[])
        for item in context.methodology_progress:
            if item["status"] == "pending":
                return CandidateBatch(
                    candidates=[
                        CandidateDraft(
                            agent_id=spec.agent_id,
                            target_node_ids=[target_id],
                            plan_step=PlanStep(
                                step_id=f"reference-{item['reference_id']}-{item['next_offset_chars']}",
                                capability="analysis",
                                operation="file_read",
                                tool_id=ToolId("analysis.read_file"),
                                target=AssetTarget(asset_id=AssetId(self.library.asset_ref)),
                                arguments={
                                    "path": item["path"],
                                    "offset_chars": item["next_offset_chars"],
                                    "max_chars": self.library.read_chars,
                                },
                                reason="승인된 참고 자료 조회입니다. 실행 허가는 아닙니다.",
                                assessment_kind="observation",
                            ),
                        )
                    ]
                )
        return CandidateBatch(candidates=[])


def reference_agent() -> tuple[AgentSpec, AgentDefinition]:
    return (
        AgentSpec(
            agent_id=REFERENCE_AGENT_ID,
            name="Methodology Reference Agent",
            capabilities=["analysis"],
            planner_kind="playbook",
        ),
        AgentDefinition(
            agent_id=REFERENCE_AGENT_ID,
            role="reference",
            name="Methodology Reference Agent",
            capabilities=["analysis"],
            provider_id=REFERENCE_PROVIDER_ID,
            priority_band=0,
            context_profile=ContextProfile(profile_id="methodology", include_node_types=[]),
        ),
    )
