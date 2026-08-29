from __future__ import annotations

import shutil
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from cicd_redteam_agent.application.objective import (
    compile_objective_goal,
    normalize_objective,
    objective_hash,
)
from cicd_redteam_agent.application.report import write_report
from cicd_redteam_agent.application.state import create_initial_state
from cicd_redteam_agent.domain.attack_graph import compose_global_attack_paths
from cicd_redteam_agent.domain.attacker_control import assess_attacker_control, derive_attack_paths
from cicd_redteam_agent.domain.aws_oidc import derive_aws_oidc_trust_assessments
from cicd_redteam_agent.domain.evidence_requirement import annotate_external_evidence_requirements
from cicd_redteam_agent.domain.execution_semantics import derive_execution_semantics
from cicd_redteam_agent.domain.github_actions_parser import discover_workflows, parse_workflow
from cicd_redteam_agent.domain.identity_analysis import derive_identity_candidates
from cicd_redteam_agent.domain.investigation import GitHubEventLocator
from cicd_redteam_agent.domain.models import (
    AnalysisMode,
    AnalysisReport,
    AttackerProfile,
    ReportStatus,
    Termination,
    TraceMetadata,
)
from cicd_redteam_agent.domain.reference_tracer import trace_local_references
from cicd_redteam_agent.domain.scenario_rules import derive_additional_scenario_results
from cicd_redteam_agent.domain.security_facts import derive_security_facts
from cicd_redteam_agent.framework.auth import ModelProvider
from cicd_redteam_agent.security.redaction import redact
from cicd_redteam_agent.security.repository_boundary import RepositoryBoundary

FRAMEWORK_COMMIT = "b2653771aed383ffe92933f8f2f9b7684403af8a"


@dataclass(frozen=True)
class AnalysisConfig:
    output_dir: Path
    attacker_profile: AttackerProfile = AttackerProfile.FORK_PR
    model: str | None = None
    provider: ModelProvider = ModelProvider.AUTO
    analysis_mode: AnalysisMode | None = None
    env_file: Path | None = None
    max_rounds: int = 12
    max_tokens: int = 100_000
    max_cost: float = 2.0
    max_duration: int = 900
    objective: str | None = None
    observe_github: bool = False
    observe_aws: bool = False
    aws_profile: str | None = None
    github_run_id: int | None = None
    github_event_locator: GitHubEventLocator | None = None

    def __post_init__(self) -> None:
        if self.objective is not None:
            object.__setattr__(self, "objective", normalize_objective(self.objective))
        if self.github_run_id is not None and self.github_run_id <= 0:
            raise ValueError("github_run_id must be positive")
        if self.github_event_locator is not None and self.github_run_id is None:
            raise ValueError("github_event_locator requires github_run_id")
        if self.observe_aws and not self.observe_github:
            raise ValueError("observe_aws requires observe_github for exact GitHub OIDC claims")
        if self.aws_profile is not None and not self.observe_aws:
            raise ValueError("aws_profile requires observe_aws")

    @property
    def resolved_mode(self) -> AnalysisMode:
        if self.analysis_mode is not None:
            return self.analysis_mode
        if self.model or self.provider is ModelProvider.CODEX_CHATGPT:
            return AnalysisMode.REASONING
        return AnalysisMode.DETERMINISTIC


@dataclass(frozen=True)
class ResumeLimitOverrides:
    max_rounds: int | None = None
    max_tokens: int | None = None
    max_cost: float | None = None
    max_duration: int | None = None


def _merge_trace(source_trace: Path | None, destination: Path, *, append: bool) -> None:
    if source_trace is None or not source_trace.exists():
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not append or not destination.exists():
        shutil.copy2(source_trace, destination)
        return
    if source_trace.resolve() == destination.resolve():
        return
    with destination.open("a+b") as target, source_trace.open("rb") as source:
        if target.tell() > 0:
            target.seek(-1, 2)
            if target.read(1) != b"\n":
                target.write(b"\n")
        shutil.copyfileobj(source, target)


def _finalize_analysis(
    *,
    state,
    config: AnalysisConfig,
    mode: AnalysisMode,
    run_dir: Path,
    warnings: list[str],
    status: ReportStatus,
    reason: str,
    duration_seconds: float,
    rounds: int = 0,
    tokens: int = 0,
    cost: float = 0.0,
    detail: str | None = None,
    source_trace: Path | None = None,
    provider: str | None = None,
    runtime_metrics: dict[str, object] | None = None,
    append_trace: bool = False,
) -> tuple[AnalysisReport, Path]:
    state.termination = Termination(
        reason=reason,
        detail=detail,
        rounds=rounds,
        tokens=tokens,
        cost=cost,
        duration_seconds=duration_seconds,
    )
    run_metadata: dict[str, object] = {
        "attacker_profile": config.attacker_profile,
        "max_rounds": config.max_rounds,
        "max_tokens": config.max_tokens,
        "max_cost": config.max_cost,
        "max_duration": config.max_duration,
        "provider": provider or config.provider,
        "analysis_mode": mode,
        "framework_commit": FRAMEWORK_COMMIT,
        "repository_commit": state.repository.git_commit,
        "objective": config.objective,
        "objective_hash": objective_hash(config.objective),
        "objective_goal": compile_objective_goal(config.objective).as_dict(),
        "observe_github": config.observe_github,
        "observe_aws": config.observe_aws,
        "aws_profile": config.aws_profile,
        "github_run_id": config.github_run_id,
        "github_event_locator": (
            config.github_event_locator.model_dump(mode="json")
            if config.github_event_locator is not None
            else None
        ),
    }
    if runtime_metrics is not None:
        run_metadata["agent_runtime"] = runtime_metrics
    report = AnalysisReport(
        status=status,
        termination_reason=reason,
        state=state,
        warnings=warnings,
        trace=TraceMetadata(
            provider=provider,
            model=config.model,
            framework_commit=FRAMEWORK_COMMIT,
            trace_path=str(run_dir / "trace.jsonl"),
        ),
        run_metadata=run_metadata,
    )
    _merge_trace(source_trace, run_dir / "trace.jsonl", append=append_trace)
    write_report(report, run_dir)
    return report, run_dir


async def analyze_repository(
    repo_path: str | Path, config: AnalysisConfig
) -> tuple[AnalysisReport, Path]:
    started = time.monotonic()
    mode = config.resolved_mode
    if config.objective is not None and mode is AnalysisMode.DETERMINISTIC:
        raise ValueError("analysis objective requires reasoning mode")
    if config.observe_github and mode is AnalysisMode.DETERMINISTIC:
        raise ValueError("--observe-github requires reasoning mode")
    if config.github_run_id is not None and not config.observe_github:
        raise ValueError("--github-run-id requires --observe-github")
    if config.github_event_locator is not None and not config.observe_github:
        raise ValueError("GitHub event provenance requires --observe-github")
    if (
        mode is AnalysisMode.REASONING
        and not config.model
        and config.provider is not ModelProvider.CODEX_CHATGPT
    ):
        raise ValueError("reasoning analysis requires --model or --provider codex-chatgpt")
    boundary = RepositoryBoundary(repo_path)
    state = create_initial_state(boundary)
    warnings: list[str] = []
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    run_dir = config.output_dir.resolve() / run_id

    def finalize(
        status: ReportStatus,
        reason: str,
        *,
        rounds: int = 0,
        tokens: int = 0,
        cost: float = 0.0,
        detail: str | None = None,
        source_trace: Path | None = None,
        provider: str | None = None,
        runtime_metrics: dict[str, object] | None = None,
    ) -> tuple[AnalysisReport, Path]:
        return _finalize_analysis(
            state=state,
            config=config,
            mode=mode,
            run_dir=run_dir,
            warnings=warnings,
            status=status,
            reason=reason,
            duration_seconds=time.monotonic() - started,
            rounds=rounds,
            tokens=tokens,
            cost=cost,
            detail=detail,
            source_trace=source_trace,
            provider=provider,
            runtime_metrics=runtime_metrics,
        )

    try:
        for workflow_path in discover_workflows(boundary):
            result = parse_workflow(boundary, workflow_path)
            state.workflows.append(result.workflow)
            state.evidence.update(result.evidence)
            state.secret_references.extend(result.secret_references)
            warnings.extend(result.workflow.parse_warnings)
        relations, reference_evidence, reference_warnings = trace_local_references(
            state.workflows, boundary
        )
        state.relations.extend(relations)
        state.evidence.update(reference_evidence)
        warnings.extend(reference_warnings)
        state.assessments = assess_attacker_control(state, config.attacker_profile)
        execution_semantics = derive_execution_semantics(state, config.attacker_profile)
        state.trigger_contexts = execution_semantics.trigger_contexts
        state.job_execution_contexts = execution_semantics.job_contexts
        state.condition_evaluations = execution_semantics.condition_evaluations
        state.relations.extend(execution_semantics.relations)
        state.aws_oidc_trust_assessments = derive_aws_oidc_trust_assessments(state)
        security_facts = derive_security_facts(state, config.attacker_profile)
        state.relations.extend(security_facts)
        annotate_external_evidence_requirements(state)
        state.pending_questions = sorted(
            {
                item
                for relation in [*execution_semantics.relations, *security_facts]
                for item in relation.missing_evidence
            }
        )
        state.identities = derive_identity_candidates(state)
        if mode is AnalysisMode.DETERMINISTIC:
            state.findings, state.paths = derive_attack_paths(state)
            additional_findings, additional_paths = derive_additional_scenario_results(state)
            state.findings.extend(additional_findings)
            state.paths.extend(additional_paths)
            graph_relations, global_paths = compose_global_attack_paths(
                state, config.attacker_profile
            )
            state.relations.extend(graph_relations)
            state.paths.extend(global_paths)
    except Exception as exc:
        detail = f"{type(exc).__name__}: {redact(str(exc))}"
        warnings.append(f"Deterministic analysis failed: {detail}")
        return finalize(
            ReportStatus.FAILED,
            "deterministic_analysis_failed",
            detail=detail,
        )

    if mode is AnalysisMode.REASONING:
        from cicd_redteam_agent.framework.hackingbuddy_adapter import run_agent_analysis

        agent_result = await run_agent_analysis(state, boundary, config, run_dir)
        warnings.extend(agent_result.warnings)
        return finalize(
            agent_result.status,
            agent_result.reason,
            rounds=agent_result.rounds,
            tokens=agent_result.tokens,
            cost=agent_result.cost,
            source_trace=agent_result.trace_path,
            provider=agent_result.provider,
            runtime_metrics=agent_result.runtime_metrics,
            detail=None,
        )

    if config.model or config.provider is ModelProvider.CODEX_CHATGPT:
        warnings.append("Deterministic mode selected; configured model was not used.")
    else:
        warnings.append("No model configured; deterministic analysis completed without LLM review.")
    return finalize(ReportStatus.COMPLETE, "deterministic_analysis_complete")


async def resume_analysis(
    run_dir: str | Path,
    *,
    env_file: Path | None = None,
    limits: ResumeLimitOverrides | None = None,
) -> tuple[AnalysisReport, Path]:
    from cicd_redteam_agent.framework.hackingbuddy_adapter import run_agent_analysis
    from cicd_redteam_agent.framework.runtime_support import (
        PROMPT_VERSION,
        EventJournal,
        load_checkpoint,
    )

    resolved_run_dir = Path(run_dir).expanduser().resolve()
    checkpoint = load_checkpoint(resolved_run_dir / "checkpoint.json")
    if checkpoint.reason == "proposal_validated" or checkpoint.resume_phase == "TERMINAL":
        raise ValueError("analysis is already complete and cannot be resumed")
    if checkpoint.prompt_version != PROMPT_VERSION:
        raise ValueError("system prompt version changed; start a new analysis run")

    saved = checkpoint.config
    required = {
        "attacker_profile",
        "analysis_mode",
        "max_rounds",
        "max_tokens",
        "max_cost",
        "max_duration",
        "objective",
        "objective_hash",
        "objective_goal",
    }
    missing = sorted(required - saved.keys())
    if missing:
        raise ValueError(f"checkpoint is missing resume configuration: {', '.join(missing)}")
    if AnalysisMode(saved["analysis_mode"]) is not AnalysisMode.REASONING:
        raise ValueError("only reasoning-mode analysis runs can be resumed")
    saved_objective = saved["objective"]
    if saved_objective is not None and not isinstance(saved_objective, str):
        raise ValueError("checkpoint analysis objective is invalid")
    if objective_hash(saved_objective) != saved["objective_hash"]:
        raise ValueError("checkpoint analysis objective hash mismatch")
    expected_goal = compile_objective_goal(saved_objective).as_dict()
    if saved["objective_goal"] != expected_goal:
        raise ValueError("checkpoint compiled objective goal mismatch")

    overrides = limits or ResumeLimitOverrides()
    config = AnalysisConfig(
        output_dir=resolved_run_dir.parent,
        attacker_profile=AttackerProfile(saved["attacker_profile"]),
        model=checkpoint.model,
        provider=ModelProvider(checkpoint.provider),
        analysis_mode=AnalysisMode.REASONING,
        env_file=env_file,
        max_rounds=overrides.max_rounds or int(saved["max_rounds"]),
        max_tokens=overrides.max_tokens or int(saved["max_tokens"]),
        max_cost=overrides.max_cost or float(saved["max_cost"]),
        max_duration=overrides.max_duration or int(saved["max_duration"]),
        objective=saved_objective,
        observe_github=bool(saved.get("observe_github", False)),
        observe_aws=bool(saved.get("observe_aws", False)),
        aws_profile=(str(saved["aws_profile"]) if saved.get("aws_profile") else None),
        github_run_id=(
            int(saved["github_run_id"]) if saved.get("github_run_id") is not None else None
        ),
        github_event_locator=(
            GitHubEventLocator.model_validate(saved["github_event_locator"])
            if saved.get("github_event_locator") is not None
            else None
        ),
    )
    consumed = {
        "max_rounds": (config.max_rounds, checkpoint.rounds),
        "max_tokens": (config.max_tokens, checkpoint.tokens),
        "max_cost": (config.max_cost, checkpoint.cost),
        "max_duration": (config.max_duration, checkpoint.active_duration_seconds),
    }
    exhausted = [name for name, (maximum, used) in consumed.items() if maximum <= used]
    if exhausted:
        raise ValueError("resume limits must exceed cumulative usage: " + ", ".join(exhausted))
    if (
        config.provider is ModelProvider.CODEX_CHATGPT
        and env_file is not None
        and not config.observe_github
    ):
        raise ValueError("--env-file is not used with the saved Codex login provider")

    boundary = RepositoryBoundary(checkpoint.state.repository.root)
    current_repository = create_initial_state(boundary).repository
    saved_repository = checkpoint.state.repository
    if current_repository.git_commit != saved_repository.git_commit:
        raise ValueError("repository commit changed after checkpoint; start a new analysis run")
    if saved_repository.dirty is True or current_repository.dirty is True:
        raise ValueError("dirty repositories cannot be safely resumed; start a new analysis run")

    journal = EventJournal(
        resolved_run_dir / "events.jsonl",
        attempt_id=checkpoint.attempt_id + 1,
    )
    if journal.last_sequence < checkpoint.committed_event_seq:
        raise ValueError("execution journal is older than the checkpoint")

    state = checkpoint.state
    state.termination = None
    started = time.monotonic()
    agent_result = await run_agent_analysis(
        state,
        boundary,
        config,
        resolved_run_dir,
        resume_checkpoint=checkpoint,
    )
    warnings = [
        f"Resumed from checkpoint attempt {checkpoint.attempt_id} at round {checkpoint.rounds}."
    ]
    warnings.extend(agent_result.warnings)
    return _finalize_analysis(
        state=state,
        config=config,
        mode=AnalysisMode.REASONING,
        run_dir=resolved_run_dir,
        warnings=warnings,
        status=agent_result.status,
        reason=agent_result.reason,
        duration_seconds=max(
            agent_result.duration_seconds,
            checkpoint.active_duration_seconds + (time.monotonic() - started),
        ),
        rounds=agent_result.rounds,
        tokens=agent_result.tokens,
        cost=agent_result.cost,
        source_trace=agent_result.trace_path,
        provider=agent_result.provider,
        runtime_metrics=agent_result.runtime_metrics,
        append_trace=True,
    )
