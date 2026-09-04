from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from hackingBuddyGPT.utils.console import Console
from hackingBuddyGPT.utils.limits import Limits
from hackingBuddyGPT.utils.llm import LiteLLM
from hackingBuddyGPT.utils.logging import JsonlLogger

from cicd_redteam_agent.application.objective import compile_objective_goal, objective_hash
from cicd_redteam_agent.domain.models import AnalysisState, ReportStatus
from cicd_redteam_agent.framework.auth import (
    ModelProvider,
    resolve_github_observer_token,
    resolve_litellm_credential,
)
from cicd_redteam_agent.framework.aws_resolvers import AwsIamResolver
from cicd_redteam_agent.framework.capabilities import CapabilityContext, build_capabilities
from cicd_redteam_agent.framework.cicd_agent import CICDChatAgent
from cicd_redteam_agent.framework.cicd_usecase import CICDUseCase
from cicd_redteam_agent.framework.evidence_resolver import ResolverRegistry
from cicd_redteam_agent.framework.github_resolvers import build_github_resolver_registry
from cicd_redteam_agent.framework.investigation_runtime import (
    create_red_team_session,
    prepare_resume_revalidation,
    synchronize_analysis_state,
)
from cicd_redteam_agent.framework.local_resolvers import LocalRuntimeTelemetryResolver
from cicd_redteam_agent.framework.runtime_support import (
    AgentRuntimeMetrics,
    CheckpointWriter,
    EventJournal,
    ResumeCheckpoint,
    RunLease,
    restore_limits,
)
from cicd_redteam_agent.security.repository_boundary import RepositoryBoundary

FRAMEWORK_COMMIT = "b2653771aed383ffe92933f8f2f9b7684403af8a"


@dataclass
class AgentRunResult:
    status: ReportStatus
    reason: str
    rounds: int
    tokens: int
    cost: float
    warnings: list[str]
    trace_path: Path | None = None
    provider: str | None = None
    runtime_metrics: dict[str, object] | None = None
    checkpoint_path: Path | None = None
    attempt_id: int = 1
    duration_seconds: float = 0.0


async def run_agent_analysis(
    state: AnalysisState,
    boundary: RepositoryBoundary,
    config,
    run_dir: Path,
    *,
    resume_checkpoint: ResumeCheckpoint | None = None,
) -> AgentRunResult:
    with RunLease(run_dir / ".run.lock"):
        return await _run_agent_analysis_locked(
            state,
            boundary,
            config,
            run_dir,
            resume_checkpoint=resume_checkpoint,
        )


async def _run_agent_analysis_locked(
    state: AnalysisState,
    boundary: RepositoryBoundary,
    config,
    run_dir: Path,
    *,
    resume_checkpoint: ResumeCheckpoint | None,
) -> AgentRunResult:
    trace_dir = config.output_dir.resolve() / ".agent-traces"
    trace_dir.mkdir(parents=True, exist_ok=True)
    logger_console = Console()
    # Rich expands ``:secret:`` inside canonical node IDs such as
    # ``credential:secret:<id>`` into U+3299.  Legacy Korean Windows consoles use CP949 and
    # cannot encode that emoji, so a perfectly valid tool result used to crash while being
    # displayed.  Disable emoji expansion only; the canonical IDs and JSON trace stay unchanged.
    logger_console._emoji = False
    logger = JsonlLogger(
        console=logger_console,
        log_dir=str(trace_dir),
        tag="cicd-redteam-agent",
    )
    limits = Limits(
        max_rounds=config.max_rounds,
        max_tokens=config.max_tokens,
        max_cost=config.max_cost,
        max_duration=config.max_duration,
    )
    attempt_id = resume_checkpoint.attempt_id + 1 if resume_checkpoint else 1
    if resume_checkpoint is not None:
        restore_limits(limits, resume_checkpoint)
    event_journal = EventJournal(run_dir / "events.jsonl", attempt_id=attempt_id)
    event_journal.append(
        "RESUME_STARTED" if resume_checkpoint else "RUN_STARTED",
        round=limits.rounds,
        payload={
            "provider": resume_checkpoint.provider if resume_checkpoint else config.provider.value,
            "model": config.model,
            "checkpoint_event_seq": (
                resume_checkpoint.committed_event_seq if resume_checkpoint else 0
            ),
        },
    )
    objective_goal = compile_objective_goal(config.objective)
    objective_goal_payload = objective_goal.as_dict()
    runtime_objective_goal = objective_goal if config.objective is not None else None
    runtime_objective_goal_payload = (
        objective_goal_payload if config.objective is not None else None
    )
    investigation_session = None
    environment_observer: ResolverRegistry | None = None
    observation_enabled = bool(config.observe_github or config.runtime_telemetry_file is not None)
    if observation_enabled:
        environment_observer = ResolverRegistry()
        if resume_checkpoint is not None:
            if resume_checkpoint.investigation_session is None:
                raise ValueError(
                    "observation-enabled resume requires a persisted investigation session"
                )
            investigation_session = resume_checkpoint.investigation_session.model_copy(deep=True)
            if investigation_session.github_run_id != config.github_run_id:
                raise ValueError("checkpoint GitHub workflow run identity does not match config")
            if investigation_session.github_event_locator != config.github_event_locator:
                raise ValueError("checkpoint GitHub event provenance locator does not match config")
            investigation_session.analysis_state = state.model_copy(deep=True)
            prepare_resume_revalidation(
                investigation_session,
                boundary=boundary,
                profile=config.attacker_profile,
            )
            synchronize_analysis_state(state, investigation_session.analysis_state)
        else:
            investigation_session = create_red_team_session(
                state,
                github_run_id=config.github_run_id,
                github_event_locator=config.github_event_locator,
            )

    if config.observe_github:
        github_token = resolve_github_observer_token(
            config.env_file,
            untrusted_repository_root=boundary.root,
        )
        if github_token is None:
            raise ValueError(
                "--observe-github requires GH_TOKEN or GITHUB_TOKEN in trusted operator config"
            )
        environment_observer = build_github_resolver_registry(
            boundary,
            token=github_token,
        )
        if config.observe_aws:
            environment_observer.register(AwsIamResolver.from_profile(config.aws_profile))
    if config.runtime_telemetry_file is not None:
        assert environment_observer is not None
        environment_observer.register(
            LocalRuntimeTelemetryResolver(
                path=config.runtime_telemetry_file,
                untrusted_repository_root=boundary.root,
            )
        )
    if config.provider is ModelProvider.CODEX_CHATGPT:
        from cicd_redteam_agent.framework.codex_llm import CodexChatGPTLLM

        llm = CodexChatGPTLLM(
            model=config.model,
            context_size=min(config.max_tokens, 128_000),
            state=state,
            objective=config.objective,
            objective_goal=runtime_objective_goal_payload,
        )
        provider = "codex-chatgpt"
    else:
        credential = resolve_litellm_credential(
            config.provider,
            config.model,
            env_file=config.env_file,
            untrusted_repository_root=boundary.root,
        )
        llm = LiteLLM(
            api_key=credential.api_key,
            model=config.model,
            context_size=min(config.max_tokens, 128_000),
            api_retries=0,
        )
        provider = credential.provider.value
    llm.init()
    try:
        context = CapabilityContext(
            state=state,
            boundary=boundary,
            analysis_mode=config.resolved_mode,
            objective_goal=runtime_objective_goal,
        )
        prompt_path = Path(__file__).parents[1] / "prompts" / "cicd_security.md"
        prompt = prompt_path.read_text(encoding="utf-8")
        runtime_metrics = (
            resume_checkpoint.runtime_metrics if resume_checkpoint else AgentRuntimeMetrics()
        )
        agent = CICDChatAgent(
            log=logger,
            llm=llm,
            prompt=prompt,
            state=state,
            objective=config.objective,
            objective_goal=runtime_objective_goal_payload,
            runtime_metrics=runtime_metrics,
            event_journal=event_journal,
        )
        agent._capabilities = build_capabilities(context)
        await agent.init()
        await logger.start_run(
            "cicd-redteam-analysis",
            (
                f"model={config.model}; framework_commit={FRAMEWORK_COMMIT}; "
                f"repository={state.repository.repository_id}"
            ),
        )
        checkpoint_path = run_dir / "checkpoint.json"
        checkpoint_writer = CheckpointWriter(
            path=checkpoint_path,
            state=state,
            provider=provider,
            model=config.model,
            metrics=runtime_metrics,
            journal=event_journal,
            attempt_id=attempt_id,
            config={
                "attacker_profile": config.attacker_profile.value,
                "analysis_mode": config.resolved_mode.value,
                "max_rounds": config.max_rounds,
                "max_tokens": config.max_tokens,
                "max_cost": config.max_cost,
                "max_duration": config.max_duration,
                "repository_commit": state.repository.git_commit,
                "repository_dirty": state.repository.dirty,
                "objective": config.objective,
                "objective_hash": objective_hash(config.objective),
                "objective_goal": objective_goal_payload,
                "observe_github": config.observe_github,
                "observe_aws": config.observe_aws,
                "runtime_telemetry_file": (
                    str(config.runtime_telemetry_file)
                    if config.runtime_telemetry_file is not None
                    else None
                ),
                "aws_profile": config.aws_profile,
                "github_run_id": config.github_run_id,
            },
            investigation_session=investigation_session,
        )
        usecase = CICDUseCase(
            agent=agent,
            context=context,
            limits=limits,
            checkpoint_writer=checkpoint_writer,
            event_journal=event_journal,
            investigation_session=investigation_session,
            environment_observer=environment_observer,
            attacker_profile=(
                config.attacker_profile if investigation_session is not None else None
            ),
        )
        reason = await usecase.run()
        status = ReportStatus.COMPLETE if reason == "proposal_validated" else ReportStatus.PARTIAL
        # Avoid JsonlLogger.run_was_success(), whose generic state is "got root".
        logger._finish_run("OK" if status is ReportStatus.COMPLETE else "ERROR", reason)
        event_journal.append(
            "RUN_TERMINATED",
            round=limits.rounds,
            payload={"reason": reason, "status": status.value},
        )
        trace_path = Path(logger._writer.path) if logger._writer and logger._writer.path else None
        return AgentRunResult(
            status=status,
            reason=reason,
            rounds=limits.rounds,
            tokens=limits.tokens,
            cost=limits.cost,
            warnings=usecase.warnings,
            trace_path=trace_path,
            provider=provider,
            runtime_metrics=runtime_metrics.to_dict(),
            checkpoint_path=checkpoint_path,
            attempt_id=attempt_id,
            duration_seconds=(limits.duration.total_seconds() if limits.duration else 0.0),
        )
    finally:
        close = getattr(llm, "close", None)
        if close is not None:
            close()
