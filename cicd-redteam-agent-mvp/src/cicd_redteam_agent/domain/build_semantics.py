from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from urllib.parse import urlparse

from cicd_redteam_agent.domain.github_actions_parser import stable_id
from cicd_redteam_agent.domain.models import (
    AnalysisState,
    Evidence,
    EvidenceProvider,
    EvidenceRequirementKind,
    EvidenceRequirementRef,
    Relation,
    Step,
    TriState,
)
from cicd_redteam_agent.security.redaction import redact
from cicd_redteam_agent.security.repository_boundary import (
    RepositoryBoundary,
    RepositoryBoundaryError,
)

# This layer is intentionally package-manager/build-semantic, not fixture semantic.  The first
# supported adapter is the Node package lifecycle because install hooks are a common CI/CD
# supply-chain execution boundary.  Additional adapters can emit the same Relation vocabulary.
PACKAGE_INSTALL = re.compile(r"^(?:npm|pnpm|yarn)\s+(?:ci|install)(?:\s|$)", re.IGNORECASE)
CHANGE_DIRECTORY = re.compile(r"^(?:cd|pushd)\s+(.+?)\s*$", re.IGNORECASE)
STATIC_PATH = re.compile(r"^[A-Za-z0-9_./\\ -]+$")
SCRIPT_EXECUTION = re.compile(
    r"(?<![\w-])(?:node|python(?:3)?|bash|sh|pwsh(?:\.exe)?(?:\s+-File)?)\s+"
    r"[\"']?([A-Za-z0-9_./\\-]+\.(?:js|cjs|mjs|ts|py|sh|ps1))[\"']?",
    re.IGNORECASE,
)
NETWORK_LITERAL_CALL = re.compile(
    r"(?:"
    r"(?:https?|axios)\s*\.\s*(?:get|request|post|put|patch|delete)\s*\(\s*"
    r"|fetch\s*\(\s*"
    r")[\"'](https?://[^\"'\s)]+)[\"']",
    re.IGNORECASE | re.MULTILINE,
)
FILESYSTEM_WRITE = re.compile(
    r"(?:\bfs\s*\.\s*)?(?:writeFileSync|writeFile|appendFileSync|appendFile|"
    r"truncateSync|truncate|renameSync|rename)\s*\(",
    re.IGNORECASE,
)
RELATIVE_WRITE_LITERAL = re.compile(
    r"(?:writeFileSync|writeFile|appendFileSync|appendFile|truncateSync|truncate)\s*\(\s*"
    r"[\"'](\.?\.?[/\\][^\"']+)[\"']",
    re.IGNORECASE,
)
PROCESS_SPAWN = re.compile(
    r"(?:\bchild_process\s*\.\s*)?(?:execSync|exec|spawnSync|spawn|fork)\s*\(",
    re.IGNORECASE,
)

NODE_INSTALL_LIFECYCLES = (
    "preinstall",
    "install",
    "postinstall",
    "prepublish",
    "preprepare",
    "prepare",
    "postprepare",
)


def _relation(
    source_id: str,
    relation_type: str,
    target_id: str,
    evidence_ids: list[str],
    *,
    category: str,
    requirements: list[EvidenceRequirementRef] | None = None,
) -> Relation:
    return Relation(
        source_id=source_id,
        relation_type=relation_type,
        target_id=target_id,
        category=category,
        verdict=TriState.TRUE,
        evidence_ids=sorted(set(evidence_ids)),
        evidence_requirements=requirements or [],
    )


def _evidence(
    path: str,
    text: str,
    *,
    kind: str,
    needle: str | None = None,
    field_path: str = "$",
) -> Evidence:
    lines = text.splitlines() or [""]
    line_number = 1
    if needle:
        for index, line in enumerate(lines, 1):
            if needle in line:
                line_number = index
                break
    excerpt = redact(lines[line_number - 1])[:1000]
    return Evidence(
        evidence_id=stable_id("ev", path, kind, str(line_number), excerpt),
        kind=kind,
        source_path=path,
        line_start=line_number,
        line_end=line_number,
        field_path=field_path,
        redacted_excerpt=excerpt,
        content_hash=hashlib.sha256(lines[line_number - 1].encode()).hexdigest(),
    )


def _inside_repository(
    boundary: RepositoryBoundary,
    base_directory: str,
    raw_path: str,
    *,
    require_directory: bool = False,
) -> str | None:
    """Resolve an internal relative path while allowing ``../`` inside the repository.

    ``RepositoryBoundary.resolve`` deliberately rejects parent syntax at its public boundary.  A
    package manifest, however, legitimately uses e.g. ``file:../shared``.  Resolve first, then
    prove the canonical target remains beneath the trusted repository root before converting it
    back to a parent-free relative path consumed by ``RepositoryBoundary.read_text``.
    """

    if not raw_path or not STATIC_PATH.fullmatch(raw_path) or "${{" in raw_path:
        return None
    normalized = raw_path.strip().strip('"').strip("'").replace("\\", "/")
    try:
        candidate = (boundary.root / base_directory / normalized).resolve(strict=True)
        relative = candidate.relative_to(boundary.root)
    except (OSError, ValueError):
        return None
    if ".git" in relative.parts:
        return None
    if require_directory and not candidate.is_dir():
        return None
    if not require_directory and not candidate.is_file():
        return None
    return relative.as_posix()


def _static_step_directory(step: Step) -> str | None:
    raw = (step.working_directory or ".").strip()
    if not raw or "${{" in raw or not STATIC_PATH.fullmatch(raw):
        return None
    raw = raw.strip('"').strip("'").replace("\\", "/")
    return raw.removeprefix("./") or "."


def _segments(run: str) -> list[str]:
    # Build commands in workflow YAML commonly use one command per line or ``cd dir && cmd``.
    # We intentionally do not execute a shell parser: only simple static separators are accepted.
    return [item.strip() for item in re.split(r"(?:\r?\n|&&|;)", run) if item.strip()]


def _package_install_directories(step: Step) -> list[str]:
    if not step.run:
        return []
    current = _static_step_directory(step)
    if current is None:
        return []
    installs: list[str] = []
    for segment in _segments(step.run):
        change = CHANGE_DIRECTORY.fullmatch(segment)
        if change:
            target = change.group(1).strip().strip('"').strip("'").replace("\\", "/")
            if not target or "${{" in target or not STATIC_PATH.fullmatch(target):
                current = ""
                continue
            if target.startswith("/") or re.match(r"^[A-Za-z]:", target):
                current = ""
                continue
            base = Path(current or ".")
            current = (base / target).as_posix()
            continue
        if current and PACKAGE_INSTALL.search(segment):
            installs.append(current)
    return list(dict.fromkeys(installs))


def _read_json(boundary: RepositoryBoundary, path: str) -> tuple[dict[str, object], str] | None:
    try:
        text = boundary.read_text(path)
        payload = json.loads(text)
    except (RepositoryBoundaryError, json.JSONDecodeError):
        return None
    return (payload, text) if isinstance(payload, dict) else None


def _local_dependencies(payload: dict[str, object]) -> list[tuple[str, str]]:
    resolved: list[tuple[str, str]] = []
    for section_name in ("dependencies", "devDependencies", "optionalDependencies"):
        section = payload.get(section_name)
        if not isinstance(section, dict):
            continue
        for name, raw_value in section.items():
            if not isinstance(name, str) or not isinstance(raw_value, str):
                continue
            lowered = raw_value.casefold()
            if lowered.startswith("file:") or lowered.startswith("link:"):
                resolved.append((name, raw_value.split(":", 1)[1]))
    return sorted(set(resolved))


def _script_files(command: str) -> list[str]:
    return list(dict.fromkeys(match.group(1) for match in SCRIPT_EXECUTION.finditer(command)))


def _runtime_requirement(state: AnalysisState) -> EvidenceRequirementRef:
    return EvidenceRequirementRef(
        kind=EvidenceRequirementKind.RUNTIME_TELEMETRY,
        provider=EvidenceProvider.LOCAL,
        subject_id=f"{state.repository.repository_id}:runtime",
    )


def _network_targets(text: str) -> list[tuple[str, str]]:
    targets: list[tuple[str, str]] = []
    for match in NETWORK_LITERAL_CALL.finditer(text):
        raw_url = match.group(1)
        parsed = urlparse(raw_url)
        if not parsed.hostname:
            continue
        default_port = 443 if parsed.scheme.casefold() == "https" else 80
        port = parsed.port or default_port
        targets.append((f"network:{parsed.hostname.casefold()}:{port}", raw_url))
    return list(dict.fromkeys(targets))


def derive_build_semantics(
    state: AnalysisState,
    boundary: RepositoryBoundary,
) -> tuple[list[Relation], dict[str, Evidence], list[str]]:
    """Extract repository-local package lifecycle execution and security side effects.

    The extractor is deliberately execution-free.  It follows only statically resolvable build
    commands, repository-local package dependencies, lifecycle hooks, and literal repository
    scripts.  Runtime telemetry is a separate optional corroborating Observation.
    """

    relations: list[Relation] = []
    evidence: dict[str, Evidence] = {}
    warnings: list[str] = []
    runtime_requirement = _runtime_requirement(state)

    for workflow in state.workflows:
        for job in workflow.jobs:
            for step in job.steps:
                for install_directory in _package_install_directories(step):
                    manifest_path = _inside_repository(
                        boundary,
                        ".",
                        f"{install_directory}/package.json",
                    )
                    if manifest_path is None:
                        continue
                    parsed_manifest = _read_json(boundary, manifest_path)
                    if parsed_manifest is None:
                        warnings.append(
                            f"Cannot parse package manifest used by {step.step_id}: {manifest_path}"
                        )
                        continue
                    manifest, manifest_text = parsed_manifest
                    manifest_ev = _evidence(
                        manifest_path,
                        manifest_text,
                        kind="package_manifest",
                    )
                    evidence[manifest_ev.evidence_id] = manifest_ev
                    manifest_node = f"file:{manifest_path}"
                    relations.append(
                        _relation(
                            step.step_id,
                            "step_runs_package_install",
                            manifest_node,
                            [*step.evidence_ids, manifest_ev.evidence_id],
                            category="execution",
                        )
                    )

                    manifest_directory = Path(manifest_path).parent.as_posix()
                    if manifest_directory == ".":
                        manifest_directory = "."
                    for dependency_name, dependency_path in _local_dependencies(manifest):
                        dependency_directory = _inside_repository(
                            boundary,
                            manifest_directory,
                            dependency_path,
                            require_directory=True,
                        )
                        if dependency_directory is None:
                            continue
                        dependency_manifest_path = _inside_repository(
                            boundary,
                            dependency_directory,
                            "package.json",
                        )
                        if dependency_manifest_path is None:
                            continue
                        dependency_parsed = _read_json(boundary, dependency_manifest_path)
                        if dependency_parsed is None:
                            continue
                        dependency_manifest, dependency_text = dependency_parsed
                        dependency_ev = _evidence(
                            manifest_path,
                            manifest_text,
                            kind="local_package_dependency",
                            needle=dependency_name,
                            field_path=f"$.dependencies.{dependency_name}",
                        )
                        evidence[dependency_ev.evidence_id] = dependency_ev
                        dep_manifest_ev = _evidence(
                            dependency_manifest_path,
                            dependency_text,
                            kind="package_manifest",
                        )
                        evidence[dep_manifest_ev.evidence_id] = dep_manifest_ev
                        dependency_manifest_node = f"file:{dependency_manifest_path}"
                        relations.append(
                            _relation(
                                manifest_node,
                                "package_manifest_declares_local_dependency",
                                dependency_manifest_node,
                                [dependency_ev.evidence_id, dep_manifest_ev.evidence_id],
                                category="transform",
                            )
                        )

                        scripts = dependency_manifest.get("scripts")
                        if not isinstance(scripts, dict):
                            continue
                        for lifecycle in NODE_INSTALL_LIFECYCLES:
                            command = scripts.get(lifecycle)
                            if not isinstance(command, str) or not command.strip():
                                continue
                            lifecycle_ev = _evidence(
                                dependency_manifest_path,
                                dependency_text,
                                kind="package_lifecycle",
                                needle=f'"{lifecycle}"',
                                field_path=f"$.scripts.{lifecycle}",
                            )
                            evidence[lifecycle_ev.evidence_id] = lifecycle_ev
                            for raw_script_path in _script_files(command):
                                script_path = _inside_repository(
                                    boundary,
                                    dependency_directory,
                                    raw_script_path,
                                )
                                if script_path is None:
                                    continue
                                try:
                                    script_text = boundary.read_text(script_path)
                                except RepositoryBoundaryError:
                                    continue
                                script_ev = _evidence(
                                    script_path,
                                    script_text,
                                    kind="package_lifecycle_script",
                                )
                                evidence[script_ev.evidence_id] = script_ev
                                script_node = f"file:{script_path}"
                                relations.append(
                                    _relation(
                                        dependency_manifest_node,
                                        "dependency_install_executes_lifecycle_script",
                                        script_node,
                                        [lifecycle_ev.evidence_id, script_ev.evidence_id],
                                        category="execution",
                                    )
                                )

                                for network_node, raw_url in _network_targets(script_text):
                                    network_ev = _evidence(
                                        script_path,
                                        script_text,
                                        kind="static_network_side_effect",
                                        needle=raw_url,
                                    )
                                    evidence[network_ev.evidence_id] = network_ev
                                    relations.append(
                                        _relation(
                                            script_node,
                                            "script_performs_external_network_request",
                                            network_node,
                                            [script_ev.evidence_id, network_ev.evidence_id],
                                            category="execution",
                                            requirements=[runtime_requirement],
                                        )
                                    )

                                has_workspace_anchor = "GITHUB_WORKSPACE" in script_text
                                relative_write = RELATIVE_WRITE_LITERAL.search(script_text)
                                if FILESYSTEM_WRITE.search(script_text) and (
                                    has_workspace_anchor or relative_write is not None
                                ):
                                    write_ev = _evidence(
                                        script_path,
                                        script_text,
                                        kind="static_workspace_side_effect",
                                        needle=(
                                            "writeFile"
                                            if "writeFile" in script_text
                                            else "appendFile"
                                            if "appendFile" in script_text
                                            else "truncate"
                                        ),
                                    )
                                    evidence[write_ev.evidence_id] = write_ev
                                    workspace_node = f"workspace:{state.repository.repository_id}"
                                    relations.append(
                                        _relation(
                                            script_node,
                                            "script_modifies_workspace_file",
                                            workspace_node,
                                            [script_ev.evidence_id, write_ev.evidence_id],
                                            category="transform",
                                            requirements=[runtime_requirement],
                                        )
                                    )

                                if PROCESS_SPAWN.search(script_text):
                                    process_ev = _evidence(
                                        script_path,
                                        script_text,
                                        kind="static_process_side_effect",
                                        needle="spawn" if "spawn" in script_text else "exec",
                                    )
                                    evidence[process_ev.evidence_id] = process_ev
                                    relations.append(
                                        _relation(
                                            script_node,
                                            "script_spawns_process",
                                            f"process:{script_path}",
                                            [script_ev.evidence_id, process_ev.evidence_id],
                                            category="execution",
                                            requirements=[runtime_requirement],
                                        )
                                    )

    deduplicated = {
        (item.source_id, item.relation_type, item.target_id): item for item in relations
    }
    return (
        sorted(
            deduplicated.values(),
            key=lambda item: (
                item.category or "",
                item.source_id,
                item.relation_type,
                item.target_id,
            ),
        ),
        evidence,
        warnings,
    )
