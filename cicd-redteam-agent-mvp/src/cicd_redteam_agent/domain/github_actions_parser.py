from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any

from ruamel.yaml import YAML

from cicd_redteam_agent.domain.models import Evidence, Job, SecretReference, Step, Workflow
from cicd_redteam_agent.security.redaction import redact
from cicd_redteam_agent.security.repository_boundary import RepositoryBoundary

SECRET_PATTERN = re.compile(
    r"\$\{\{\s*(?:secrets\.[A-Za-z_][A-Za-z0-9_]*|github\.token)\s*}}",
    re.IGNORECASE,
)
SCRIPT_PATTERN = re.compile(r"(?<![\w])((?:\./|scripts?/)[\w./-]+\.(?:sh|py|ps1|js|ts))")


@dataclass
class WorkflowParseResult:
    workflow: Workflow
    evidence: dict[str, Evidence] = field(default_factory=dict)
    secret_references: list[SecretReference] = field(default_factory=list)


def stable_id(prefix: str, *parts: object) -> str:
    digest = hashlib.sha256("|".join(str(part) for part in parts).encode()).hexdigest()[:16]
    return f"{prefix}_{digest}"


def discover_workflows(boundary: RepositoryBoundary) -> list[str]:
    directory = boundary.resolve(".github/workflows", must_exist=False)
    if not directory.exists():
        return []
    return sorted(
        path.relative_to(boundary.root).as_posix()
        for path in directory.iterdir()
        if path.is_file() and path.suffix.lower() in {".yml", ".yaml"}
    )


def _line_for(text: str, needle: str, start: int = 1) -> int:
    for number, line in enumerate(text.splitlines(), 1):
        if number >= start and needle in line:
            return number
    return 1


def _mapping_key_line(mapping: Any, key: object, fallback: int) -> int:
    try:
        return int(mapping.lc.key(key)[0]) + 1
    except (AttributeError, IndexError, KeyError, TypeError, ValueError):
        return fallback


def _node_line(node: Any, fallback: int) -> int:
    try:
        return int(node.lc.line) + 1
    except (AttributeError, TypeError, ValueError):
        return fallback


def _evidence(path: str, kind: str, field_path: str, excerpt: str, line: int) -> Evidence:
    safe = redact(excerpt.strip())[:1000]
    evidence_id = stable_id("ev", path, kind, field_path, line, safe)
    return Evidence(
        evidence_id=evidence_id,
        kind=kind,
        source_path=path,
        line_start=max(line, 1),
        line_end=max(line, 1),
        field_path=field_path,
        redacted_excerpt=safe,
        content_hash=hashlib.sha256(excerpt.encode()).hexdigest(),
    )


def _plain(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_plain(item) for item in value]
    return value


def _redacted_plain(value: Any) -> Any:
    plain = _plain(value)
    if isinstance(plain, dict):
        return {key: _redacted_plain(item) for key, item in plain.items()}
    if isinstance(plain, list):
        return [_redacted_plain(item) for item in plain]
    return redact(str(plain)) if plain is not None else None


def _normalize_permissions(value: Any) -> dict[str, str] | str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return {str(key): str(item) for key, item in value.items()}
    return str(value)


def _normalize_triggers(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, str):
        return {value: {}}
    if isinstance(value, list):
        return {str(item): {} for item in value}
    if isinstance(value, dict):
        return {str(key): _plain(item) if item is not None else {} for key, item in value.items()}
    return {str(value): {}}


def _collect_secret_references(
    value: Any,
    *,
    workflow_id: str,
    job_id: str,
    step_id: str | None,
    scope: str,
    evidence_id: str,
) -> list[SecretReference]:
    serialized = str(value)
    references: list[SecretReference] = []
    for match in SECRET_PATTERN.finditer(serialized):
        expression = match.group(0)
        references.append(
            SecretReference(
                reference_id=stable_id("secret", workflow_id, job_id, step_id, expression),
                expression=expression,
                scope=scope,
                workflow_id=workflow_id,
                job_id=job_id,
                step_id=step_id,
                evidence_ids=[evidence_id],
            )
        )
    return references


def parse_workflow(boundary: RepositoryBoundary, relative_path: str) -> WorkflowParseResult:
    text = boundary.read_text(relative_path)
    workflow_id = stable_id("wf", relative_path)
    evidence: dict[str, Evidence] = {}
    warnings: list[str] = []
    yaml = YAML(typ="rt")
    yaml.version = (1, 2)
    try:
        document = yaml.load(text)
    except Exception as exc:
        warning = f"Malformed YAML: {type(exc).__name__}: {exc}"
        ev = _evidence(relative_path, "parse_error", "$", warning, 1)
        evidence[ev.evidence_id] = ev
        return WorkflowParseResult(
            workflow=Workflow(
                workflow_id=workflow_id,
                path=relative_path,
                parse_warnings=[warning],
                evidence_ids=[ev.evidence_id],
            ),
            evidence=evidence,
        )
    if not isinstance(document, dict):
        warning = "Workflow root must be a mapping"
        ev = _evidence(relative_path, "parse_error", "$", warning, 1)
        evidence[ev.evidence_id] = ev
        return WorkflowParseResult(
            workflow=Workflow(
                workflow_id=workflow_id,
                path=relative_path,
                parse_warnings=[warning],
                evidence_ids=[ev.evidence_id],
            ),
            evidence=evidence,
        )

    triggers = _normalize_triggers(document.get("on"))
    workflow_permissions = _normalize_permissions(document.get("permissions"))
    workflow_evidence: list[str] = []
    for key, value in (("on", triggers), ("permissions", workflow_permissions)):
        if value:
            line = _mapping_key_line(document, key, _line_for(text, f"{key}:"))
            ev = _evidence(relative_path, key, key, text.splitlines()[line - 1], line)
            evidence[ev.evidence_id] = ev
            workflow_evidence.append(ev.evidence_id)

    jobs: list[Job] = []
    secrets: list[SecretReference] = []
    raw_jobs = document.get("jobs") or {}
    if not isinstance(raw_jobs, dict):
        warnings.append("jobs must be a mapping")
        raw_jobs = {}
    cursor = 1
    for raw_job_id, raw_job in raw_jobs.items():
        if not isinstance(raw_job, dict):
            warnings.append(f"job {raw_job_id!s} must be a mapping")
            continue
        job_id = f"{workflow_id}:job:{raw_job_id}"
        job_line = _mapping_key_line(
            raw_jobs, raw_job_id, _line_for(text, f"{raw_job_id}:", cursor)
        )
        cursor = job_line
        job_ev = _evidence(
            relative_path, "job", f"jobs.{raw_job_id}", text.splitlines()[job_line - 1], job_line
        )
        evidence[job_ev.evidence_id] = job_ev
        job_evidence_ids = [job_ev.evidence_id]
        declared = _normalize_permissions(raw_job.get("permissions"))
        if declared is not None:
            permission_line = _mapping_key_line(raw_job, "permissions", job_line)
            permission_ev = _evidence(
                relative_path,
                "permissions",
                f"jobs.{raw_job_id}.permissions",
                text.splitlines()[permission_line - 1],
                permission_line,
            )
            evidence[permission_ev.evidence_id] = permission_ev
            job_evidence_ids.append(permission_ev.evidence_id)
        if declared is not None:
            effective, source = declared, "job"
        elif workflow_permissions is not None:
            effective, source = workflow_permissions, "workflow"
        else:
            effective, source = {"__default__": "UNKNOWN"}, "repository-default-unknown"
        needs_value = raw_job.get("needs", [])
        needs = (
            [str(needs_value)]
            if isinstance(needs_value, str)
            else [str(x) for x in needs_value or []]
        )

        raw_job_env = {str(k): str(v) for k, v in (raw_job.get("env") or {}).items()}
        secrets.extend(
            _collect_secret_references(
                raw_job_env,
                workflow_id=workflow_id,
                job_id=job_id,
                step_id=None,
                scope="job-env",
                evidence_id=job_ev.evidence_id,
            )
        )
        job_uses = str(raw_job["uses"]) if raw_job.get("uses") is not None else None
        if job_uses and "${{" in job_uses:
            warnings.append(f"Dynamic reusable workflow path is UNKNOWN: jobs.{raw_job_id}.uses")

        steps: list[Step] = []
        for index, raw_step in enumerate(raw_job.get("steps") or []):
            if not isinstance(raw_step, dict):
                warnings.append(f"job {raw_job_id!s} step {index} must be a mapping")
                continue
            step_id = f"{job_id}:step:{index}"
            step_anchor = str(
                raw_step.get("name") or raw_step.get("uses") or raw_step.get("run") or "-"
            )
            step_line = _node_line(raw_step, _line_for(text, step_anchor[:80], job_line))
            step_excerpt = text.splitlines()[step_line - 1]
            step_ev = _evidence(
                relative_path,
                "step",
                f"jobs.{raw_job_id}.steps[{index}]",
                step_excerpt,
                step_line,
            )
            evidence[step_ev.evidence_id] = step_ev
            step_evidence_ids = [step_ev.evidence_id]
            field_evidence: dict[str, str] = {}
            for field_name in ("uses", "run", "with", "env"):
                if raw_step.get(field_name) is None:
                    continue
                field_line = _mapping_key_line(raw_step, field_name, step_line)
                field_ev = _evidence(
                    relative_path,
                    field_name,
                    f"jobs.{raw_job_id}.steps[{index}].{field_name}",
                    text.splitlines()[field_line - 1],
                    field_line,
                )
                evidence[field_ev.evidence_id] = field_ev
                field_evidence[field_name] = field_ev.evidence_id
                step_evidence_ids.append(field_ev.evidence_id)
            uses = str(raw_step["uses"]) if raw_step.get("uses") is not None else None
            raw_run = str(raw_step["run"]) if raw_step.get("run") is not None else None
            run = redact(raw_run) if raw_run is not None else None
            references: list[str] = []
            if uses and uses.startswith("./") and "${{" not in uses:
                references.append(uses)
            elif uses and "${{" in uses:
                warnings.append(
                    f"Dynamic action path is UNKNOWN: jobs.{raw_job_id}.steps[{index}].uses"
                )
            if raw_run and "${{" not in raw_run:
                references.extend(match.group(1) for match in SCRIPT_PATTERN.finditer(raw_run))
            raw_env = {str(k): str(v) for k, v in (raw_step.get("env") or {}).items()}
            raw_with = {str(k): _plain(v) for k, v in (raw_step.get("with") or {}).items()}
            env = {key: redact(value) for key, value in raw_env.items()}
            with_values = {key: _redacted_plain(value) for key, value in raw_with.items()}
            step = Step(
                step_id=step_id,
                name=redact(str(raw_step["name"])) if raw_step.get("name") is not None else None,
                uses=uses,
                run=run,
                shell=str(raw_step["shell"]) if raw_step.get("shell") is not None else None,
                env=env,
                **{"with": with_values},
                references=sorted(set(references)),
                evidence_ids=step_evidence_ids,
            )
            steps.append(step)
            for scope, value in (
                ("env", raw_env),
                ("with", raw_with),
                ("run", raw_run or ""),
            ):
                secrets.extend(
                    _collect_secret_references(
                        value,
                        workflow_id=workflow_id,
                        job_id=job_id,
                        step_id=step_id,
                        scope=scope,
                        evidence_id=field_evidence.get(scope, step_ev.evidence_id),
                    )
                )
        jobs.append(
            Job(
                job_id=job_id,
                uses=job_uses,
                **{"with": {str(k): _plain(v) for k, v in (raw_job.get("with") or {}).items()}},
                needs=needs,
                condition=redact(str(raw_job["if"])) if raw_job.get("if") is not None else None,
                permissions_declared=declared,
                permissions_effective=effective,
                permissions_source=source,
                environment=_redacted_plain(raw_job.get("environment")),
                steps=steps,
                evidence_ids=job_evidence_ids,
            )
        )

    return WorkflowParseResult(
        workflow=Workflow(
            workflow_id=workflow_id,
            path=relative_path,
            name=redact(str(document["name"])) if document.get("name") is not None else None,
            triggers=triggers,
            permissions=workflow_permissions,
            jobs=jobs,
            parse_warnings=warnings,
            evidence_ids=workflow_evidence,
        ),
        evidence=evidence,
        secret_references=secrets,
    )
