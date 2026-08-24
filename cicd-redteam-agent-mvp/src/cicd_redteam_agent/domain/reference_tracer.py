from __future__ import annotations

import hashlib
import re
from pathlib import Path

from ruamel.yaml import YAML

from cicd_redteam_agent.domain.github_actions_parser import stable_id
from cicd_redteam_agent.domain.models import Evidence, Relation, Workflow
from cicd_redteam_agent.security.redaction import redact
from cicd_redteam_agent.security.repository_boundary import (
    RepositoryBoundary,
    RepositoryBoundaryError,
)

NESTED_RUN = re.compile(r"(?<![\w])((?:\./|scripts?/)[\w./-]+\.(?:sh|py|ps1|js|ts))")


def _file_evidence(path: str, text: str) -> Evidence:
    excerpt = redact("\n".join(text.splitlines()[:8]))[:1000]
    evidence_id = stable_id("ev", path, "referenced_file", excerpt)
    return Evidence(
        evidence_id=evidence_id,
        kind="referenced_file",
        source_path=path,
        line_start=1,
        line_end=max(1, min(8, len(text.splitlines()))),
        field_path="$",
        redacted_excerpt=excerpt,
        content_hash=hashlib.sha256(text.encode()).hexdigest(),
    )


def _resolve_reference(boundary: RepositoryBoundary, raw: str) -> str | None:
    clean = raw[2:] if raw.startswith("./") else raw
    try:
        path = boundary.resolve(clean)
    except RepositoryBoundaryError:
        return None
    if path.is_dir():
        for name in ("action.yml", "action.yaml"):
            candidate = path / name
            if candidate.is_file():
                return candidate.relative_to(boundary.root).as_posix()
        return None
    return path.relative_to(boundary.root).as_posix() if path.is_file() else None


def trace_local_references(
    workflows: list[Workflow], boundary: RepositoryBoundary, max_depth: int = 3
) -> tuple[list[Relation], dict[str, Evidence], list[str]]:
    relations: list[Relation] = []
    evidence: dict[str, Evidence] = {}
    warnings: list[str] = []
    visited: set[str] = set()

    def visit(source_id: str, raw_ref: str, source_evidence: list[str], depth: int) -> None:
        if depth > max_depth:
            warnings.append(f"Reference depth exceeded for {raw_ref}")
            return
        target = _resolve_reference(boundary, raw_ref)
        if target is None:
            warnings.append(f"Unresolved or forbidden local reference: {raw_ref}")
            return
        if target in visited:
            relations.append(
                Relation(
                    source_id=source_id,
                    relation_type="local_reference_cycle",
                    target_id=f"file:{target}",
                    evidence_ids=source_evidence,
                )
            )
            return
        visited.add(target)
        try:
            text = boundary.read_text(target)
        except RepositoryBoundaryError as exc:
            warnings.append(f"Cannot inspect {target}: {exc}")
            return
        file_ev = _file_evidence(target, text)
        evidence[file_ev.evidence_id] = file_ev
        relations.append(
            Relation(
                source_id=source_id,
                relation_type="executes_repository_file",
                target_id=f"file:{target}",
                evidence_ids=sorted(set(source_evidence + [file_ev.evidence_id])),
            )
        )
        if Path(target).suffix.lower() not in {".yml", ".yaml"}:
            return
        yaml = YAML(typ="safe")
        yaml.version = (1, 2)
        try:
            document = yaml.load(text) or {}
        except Exception as exc:
            warnings.append(f"Malformed local action {target}: {type(exc).__name__}")
            return
        nested_steps = list((document.get("runs") or {}).get("steps") or [])
        for nested_job in (document.get("jobs") or {}).values():
            if not isinstance(nested_job, dict):
                continue
            if nested_job.get("uses"):
                nested_steps.append({"uses": nested_job.get("uses")})
            nested_steps.extend(nested_job.get("steps") or [])
        for index, step in enumerate(nested_steps):
            if not isinstance(step, dict):
                continue
            nested: list[str] = []
            uses = step.get("uses")
            run = step.get("run")
            if isinstance(uses, str) and uses.startswith("./") and "${{" not in uses:
                nested.append(uses)
            if isinstance(run, str) and "${{" not in run:
                nested.extend(match.group(1) for match in NESTED_RUN.finditer(run))
            for item in nested:
                visit(f"file:{target}:step:{index}", item, [file_ev.evidence_id], depth + 1)

    for workflow in workflows:
        for job in workflow.jobs:
            if job.uses and job.uses.startswith("./") and "${{" not in job.uses:
                visit(job.job_id, job.uses, job.evidence_ids, 1)
            elif job.uses:
                relations.append(
                    Relation(
                        source_id=job.job_id,
                        relation_type="uses_external_reusable_workflow",
                        target_id=f"external:{job.uses}",
                        evidence_ids=job.evidence_ids,
                    )
                )
            for step in job.steps:
                for reference in step.references:
                    visit(step.step_id, reference, step.evidence_ids, 1)
                if step.uses and not step.uses.startswith("./"):
                    parts = step.uses.rsplit("@", 1)
                    ref = parts[1] if len(parts) == 2 else ""
                    relations.append(
                        Relation(
                            source_id=step.step_id,
                            relation_type=(
                                "uses_external_action_sha"
                                if re.fullmatch(r"[0-9a-fA-F]{40}", ref)
                                else "uses_external_action_ref"
                            ),
                            target_id=f"external:{step.uses}",
                            evidence_ids=step.evidence_ids,
                        )
                    )
    return relations, evidence, warnings
