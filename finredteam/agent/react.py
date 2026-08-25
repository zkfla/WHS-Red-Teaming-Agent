"""ReAct 루프 — LLM 이 매 스텝 Thought → Action 하나를 내고, 코드가 그 Action 을
실행+판정해서 Observation 으로 돌려준다. 다음 스텝은 그 Observation 을 보고 또
LLM 이 정한다.

★ 여전히 LLM 은 액션만 고른다. Observation(오라클 판정 포함)은 항상 코드가
만든다 — `oracles/verifier.py` 는 순수 함수 그대로다. `execute_experiment` 가
어떤 invariant 를 검사할지도 LLM 이 정하지 않는다 — `oracles/registry.py` 가
실행 전에 고정해둔 오라클 전체 집합을 예외 없이 돈다(§설계원칙 1, 3).

후보 생성(가설, 부작용 없음)과 실제 실행(실험, 부작용 있음)을
`propose_hypothesis` / `execute_experiment` 로 분리했다 — LLM 이 뭘 하려는지
먼저 선언하고, 그래프 검증을 통과한 것만 실제로 실행된다.
"""
from __future__ import annotations

import json
import logging
import secrets
from dataclasses import dataclass, field
from typing import Any, Callable

from finredteam.agent import strategy as strategy_module
from finredteam.agent import termination
from finredteam.agent.budget import Budget
from finredteam.config import MAX_PROBES
from finredteam.knowledge.baseline import BaselineRegistry
from finredteam.knowledge.graph import KnowledgeGraph
from finredteam.llm.base import LLMClient, LLMError, Usage
from finredteam.memory.episodic import EpisodicMemory
from finredteam.memory.semantic import SemanticMemory
from finredteam.memory.summarizer import summarize
from finredteam.memory.working import WorkingMemory
from finredteam.models.core import Principal, Verdict
from finredteam.models.finding import CandidateFinding, Finding
from finredteam.models.knowledge import Fact, Lesson
from finredteam.oracles import verifier as oracle_verifier
from finredteam.planning import candidates as candidates_module
from finredteam.planning import planner as planner_module
from finredteam.planning import prioritizer
from finredteam.planning import prompts
from finredteam.planning.actions import (
    DiscoverSurfaceInput,
    ExecuteExperimentInput,
    FinishInput,
    LearnBaselineInput,
    ProposeHypothesisInput,
    ReActStep,
    RecallMemoryInput,
    parse_action_input,
)
from finredteam.planning.candidates import AttackTemplate, CandidateSkeleton
from finredteam.probing import executor as executor_module
from finredteam.probing.session import Session, SessionError
from finredteam.recon import entitlement as entitlement_module
from finredteam.recon import fingerprint as fingerprint_module
from finredteam.recon import mapper as mapper_module
from finredteam.reporting.transcript import write_transcript
from finredteam.storage.run_store import RunStore
from finredteam.targets.loader import TargetConfig

logger = logging.getLogger(__name__)

_TARGET_UNHEALTHY_THRESHOLD = 5
_SATURATED_STEPS_WITHOUT_GROWTH = 6
_CONVERGED_STEPS_WITHOUT_GROWTH = 4

Hypothesis = tuple[CandidateSkeleton, dict[str, str]]


@dataclass
class Step:
    step_no: int
    thought: str
    action: str
    action_input: dict[str, Any]
    observation: str


@dataclass
class AgentContext:
    target: TargetConfig
    session: Session
    llm: LLMClient
    store: RunStore
    budget: Budget

    principals: list[Principal] = field(default_factory=list)
    graph: KnowledgeGraph = field(default_factory=KnowledgeGraph)
    working: WorkingMemory = field(default_factory=WorkingMemory)
    semantic: SemanticMemory = field(default_factory=SemanticMemory)
    episodic: EpisodicMemory = field(default_factory=EpisodicMemory)
    baselines: BaselineRegistry = field(default_factory=BaselineRegistry)
    templates: dict[str, AttackTemplate] = field(default_factory=dict)
    pending_hypotheses: dict[str, Hypothesis] = field(default_factory=dict)

    round: int = 0  # ReAct 스텝 카운터. termination.py/budget.py 가 참조하는 이름을 그대로 쓴다.
    steps: list[Step] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    review_queue: list[Finding] = field(default_factory=list)

    _prev_edge_count: int = 0
    _steps_without_growth: int = 0
    _consecutive_request_errors: int = 0
    _finish_requested: str | None = None
    _hypothesis_counter: int = 0
    _discover_calls: int = 0

    # ── agent/strategy.py::suggest_nudge 가 요구하는 신호 ─────────────
    def has_pending_race_trials(self) -> bool:
        return False

    def graph_grew_significantly(self) -> bool:
        return self.graph.grew_significantly(self._prev_edge_count)

    def recent_violations(self) -> bool:
        return any(f.verdict == Verdict.VIOLATION for f in self.findings[-5:])

    def successful_operations(self) -> list[str]:
        ops = sorted({f.candidate.operation for f in self.findings if f.verdict == Verdict.VIOLATION})
        return ops or candidates_module.supported_operations()

    def consecutive_failures(self) -> int:
        return self.episodic.consecutive_failures()

    def least_tried_operations(self) -> list[str]:
        tried = self.episodic.tried_operations()
        all_ops = set(candidates_module.supported_operations())
        untried = sorted(all_ops - tried)
        return untried or candidates_module.supported_operations()

    def layer_untouched(self, layer: str) -> bool:
        return not any(ep.candidate.layer == layer for ep in self.episodic.all())

    # ── agent/termination.py 가 요구하는 신호 ───────────────────────
    @property
    def llm_usage(self) -> Usage:
        return self.llm.usage

    def target_unhealthy(self) -> bool:
        return self._consecutive_request_errors >= _TARGET_UNHEALTHY_THRESHOLD

    def saturated(self) -> bool:
        return self._steps_without_growth >= _SATURATED_STEPS_WITHOUT_GROWTH

    def converged(self) -> bool:
        all_ops = set(candidates_module.supported_operations())
        all_tried = bool(all_ops) and self.episodic.tried_operations() >= all_ops
        return all_tried and self._steps_without_growth >= _CONVERGED_STEPS_WITHOUT_GROWTH


def bootstrap_principals(session: Session, target: TargetConfig) -> list[Principal]:
    run_tag = secrets.token_hex(3)
    principals: list[Principal] = []
    for principal_id in target.settings.principals:
        suffix = secrets.token_hex(3)
        username = f"{principal_id}_{run_tag}_{suffix}"
        password = f"Rt!{secrets.token_urlsafe(8)}"
        principals.append(session.bootstrap(principal_id, username, password))
    return principals


def _watch_accounts_for(candidate: CandidateFinding, principal: Principal) -> list[str]:
    accounts = list(candidate.watch_accounts)
    if candidate.subject_account:
        accounts.append(candidate.subject_account)
    if principal.account_number:
        accounts.append(principal.account_number)
    return list(dict.fromkeys(a for a in accounts if a))


def _handle_discover_surface(input_: DiscoverSurfaceInput, ctx: AgentContext) -> str:
    """아직 모르는 경로를 안전하게(읽기 전용) 찔러본다 — MAX_PROBES 로 상한."""
    if ctx._discover_calls >= MAX_PROBES:
        return f"discover_surface 호출 한도({MAX_PROBES})에 도달함 — 이미 알아낸 대상으로 실험하라"
    if not input_.path.startswith("/"):
        return "path 는 '/' 로 시작해야 함"

    ctx._discover_calls += 1
    try:
        ex = ctx.session.anonymous_request(input_.method, input_.path)
    except SessionError:
        return f"{input_.method} {input_.path} 요청 실패"

    ctx.semantic.add_fact(
        Fact(
            subject=input_.path,
            predicate="discovered_status",
            object=str(ex.status_code),
            source="discover_surface",
        )
    )

    more_paths = fingerprint_module.extract_discovered_paths(ex.response_text or "")
    already_known = {input_.path}
    new_paths = sorted(p for p in more_paths if p not in already_known)
    for path in new_paths[:10]:
        ctx.semantic.add_fact(
            Fact(subject=path, predicate="discovered_status", object="unverified", source="discover_surface_scan")
        )

    summary = f"{input_.method} {input_.path} -> status {ex.status_code} (남은 호출 {MAX_PROBES - ctx._discover_calls}회)"
    if new_paths:
        summary += f"; 응답에서 추가로 발견된 경로 후보: {new_paths[:10]}"
    return summary


def _handle_propose_hypothesis(input_: ProposeHypothesisInput, ctx: AgentContext) -> str:
    skeleton = candidates_module.resolve_skeleton(
        input_.operation, input_.principal_id, input_.target, ctx.principals, ctx.graph, ctx.target
    )
    if isinstance(skeleton, str):
        logger.warning("propose_hypothesis 거절 (step=%d): %s", ctx.round, skeleton)
        return skeleton  # 검증 실패 사유를 그대로 Observation 으로 — LLM 이 다음 스텝에서 고친다

    ctx._hypothesis_counter += 1
    hypothesis_id = f"hyp_{ctx._hypothesis_counter}"
    ctx.pending_hypotheses[hypothesis_id] = (skeleton, dict(input_.params))
    return (
        f"가설 등록됨 (hypothesis_id={hypothesis_id!r}): {skeleton.method} {skeleton.path} "
        f"(주체={skeleton.principal_id}, 파라미터={input_.params}). "
        f"실행하려면 execute_experiment(hypothesis_id={hypothesis_id!r})."
    )


def _handle_execute_experiment(input_: ExecuteExperimentInput, ctx: AgentContext) -> str:
    pending = ctx.pending_hypotheses.pop(input_.hypothesis_id, None)
    if pending is None:
        logger.warning(
            "알 수 없는 hypothesis_id (step=%d): %r (대기 중: %s)",
            ctx.round,
            input_.hypothesis_id,
            list(ctx.pending_hypotheses),
        )
        return f"알 수 없는 hypothesis_id: {input_.hypothesis_id!r} — 먼저 propose_hypothesis 로 가설을 등록하라"
    skeleton, params = pending

    candidate = planner_module.build_candidate(skeleton, params, "", ctx.baselines)

    if prioritizer.already_tried_and_failed(candidate, ctx.episodic):
        logger.warning("이미 시도한 조합 재실행 거절 (step=%d): %s", ctx.round, candidate.id)
        return "이미 정확히 같은 조합(operation·주체·경로·파라미터)을 시도했고 VIOLATION 이 없었다 — 다른 파라미터나 대상을 시도하라"

    principal = next(p for p in ctx.principals if p.id == candidate.principal_id)
    watch_accounts = _watch_accounts_for(candidate, principal)

    result = executor_module.run(ctx.session, ctx.target, candidate, principal, watch_accounts, db=ctx.store.db)
    if all(code == 0 for code in result.status_codes):
        ctx._consecutive_request_errors += 1
    else:
        ctx._consecutive_request_errors = 0

    # ★ 오라클은 candidate 가 좁히지 않는다 — 등록된 전체 집합이 항상 돈다.
    outcomes = oracle_verifier.run_all_outcomes(candidate, result, ctx.graph)
    finding = oracle_verifier.verify_from_outcomes(candidate, result, outcomes)
    ctx.store.append("findings", finding.model_dump(mode="json"))

    for account in watch_accounts:
        ctx.graph.apply_observed(candidate.principal_id, f"account:{account}", "read")

    lesson = Lesson(
        candidate_summary=candidate.id,
        outcome=finding.verdict.value,
        insight=finding.reason,
        operation=candidate.operation,
    )
    ctx.episodic.record(candidate, finding, lesson)

    if finding.verdict == Verdict.VIOLATION:
        ctx.findings.append(finding)
    elif finding.verdict in (Verdict.ANOMALY, Verdict.UNKNOWN):
        ctx.review_queue.append(finding)

    noteworthy = [o for o in outcomes if o.verdict in (Verdict.VIOLATION, Verdict.ANOMALY)]
    if noteworthy:
        return "\n".join(f"{o.invariant}: {o.verdict.value} — {o.reason}" for o in noteworthy)
    passed = [o.invariant for o in outcomes if o.verdict == Verdict.PASS]
    if passed:
        return f"이상 없음 (PASS: {passed})"
    return "판정 가능한 오라클 없음 (전부 UNKNOWN) — 그래프 정보나 baseline 이 더 필요할 수 있음"


def _dispatch(step: ReActStep, ctx: AgentContext) -> str:
    parsed = parse_action_input(step.action, step.action_input)
    if isinstance(parsed, str):
        logger.warning("액션 입력 검증 실패 (step=%d, action=%s): %s", ctx.round, step.action, parsed)
        return f"입력 오류: {parsed}"

    if step.action == "observe_graph":
        menu = candidates_module.graph_menu(ctx.principals, ctx.graph, ctx.semantic)
        return json.dumps(menu, ensure_ascii=False)

    if step.action == "recall_memory":
        assert isinstance(parsed, RecallMemoryInput)
        query = parsed.query.lower()
        facts = [
            f
            for f in ctx.semantic.observed()
            if not query or query in f.subject.lower() or query in f.predicate.lower()
        ]
        payload = {
            "facts": [f"{f.subject} {f.predicate}={f.object}" for f in facts[:20]],
            "lessons": [lesson.insight for lesson in ctx.episodic.lessons()[-10:]],
        }
        return json.dumps(payload, ensure_ascii=False)

    if step.action == "learn_baseline":
        assert isinstance(parsed, LearnBaselineInput)
        op_cfg = next((o for o in ctx.target.baseline_ops() if o.get("op") == parsed.op), None)
        if op_cfg is None:
            known_ops = [o.get("op") for o in ctx.target.baseline_ops()]
            return f"알 수 없는 op: {parsed.op!r} (설정된 baseline_ops: {known_ops})"
        principals_with_account = [p for p in ctx.principals if p.account_number]
        if len(principals_with_account) < 2:
            return "baseline 학습에 필요한 계좌를 가진 주체가 2명 미만"
        payer, payee = principals_with_account[0], principals_with_account[1]
        record = ctx.baselines.learn(ctx.session, ctx.target, op_cfg, payer, counterparty=payee)
        if record is None:
            return f"{parsed.op} baseline 학습 실패 (요청 실패 또는 관측 실패)"
        return f"{parsed.op} baseline 학습 완료 (ref={record.ref_id}) — 이제 이 연산에 대한 실험은 baseline_observed 로 판정 가능"

    if step.action == "discover_surface":
        assert isinstance(parsed, DiscoverSurfaceInput)
        return _handle_discover_surface(parsed, ctx)

    if step.action == "propose_hypothesis":
        assert isinstance(parsed, ProposeHypothesisInput)
        return _handle_propose_hypothesis(parsed, ctx)

    if step.action == "execute_experiment":
        assert isinstance(parsed, ExecuteExperimentInput)
        return _handle_execute_experiment(parsed, ctx)

    if step.action == "finish":
        assert isinstance(parsed, FinishInput)
        ctx._finish_requested = parsed.reason or "llm_requested"
        return "종료 요청 접수"

    return f"알 수 없는 액션: {step.action!r}"


_SCRATCHPAD_WINDOW = 8


def _format_scratchpad(steps: list[Step], window: int = _SCRATCHPAD_WINDOW) -> str:
    """최근 스텝들의 Thought/Action/Observation 을 그대로 보여준다.

    ReAct 의 핵심은 이 누적 컨텍스트다 — 직전 1스텝만 보여주면 "방금 관찰한
    그래프 메뉴"가 두 스텝 뒤엔 사라져서 LLM 이 같은 걸 또 확인하러 간다.
    가설/실험이 분리되면서 한 실험에 스텝 2개가 드니 창을 조금 더 넓혔다.
    """
    if not steps:
        return "(아직 없음)"
    lines: list[str] = []
    for step in steps[-window:]:
        lines.append(f"[{step.step_no}] Thought: {step.thought}")
        lines.append(f"    Action: {step.action}({step.action_input})")
        lines.append(f"    Observation: {step.observation}")
    return "\n".join(lines)


def _build_observation_text(ctx: AgentContext) -> str:
    lines = [
        f"스텝: {ctx.round}/{ctx.budget.max_rounds}",
        f"그래프: entitled={len(ctx.graph.entitled_edges())} observed={len(ctx.graph.observed_edges())}",
        f"지금까지 확정된 VIOLATION: {len(ctx.findings)}건",
        f"대기 중인 가설: {list(ctx.pending_hypotheses)}",
        f"지금까지의 진행 기록:\n{_format_scratchpad(ctx.steps)}",
    ]
    nudge = strategy_module.suggest_nudge(ctx)
    if nudge:
        lines.append(nudge)
    lines.append(f"이전 시도 요약:\n{summarize(ctx.semantic, ctx.episodic)}")
    return "\n\n".join(lines)


def _finalize(store: RunStore, reason: str, usage: Usage) -> None:
    """`store.finalize()` + 사람이 읽는 트레이스(`transcript.md`) 를 항상 같이 남긴다
    (§ DEBUGGING.md 3.5) — 정상 종료·LLM 의 finish 요청·예외로 인한 중단 전부 여기를 거친다."""
    store.finalize(reason, usage=usage)
    write_transcript(store.db, store.manifest, store.run_dir)


def run(
    target: TargetConfig,
    session: Session,
    llm: LLMClient,
    store: RunStore,
    *,
    max_rounds: int | None = None,
    on_step: Callable[[Step], None] | None = None,
) -> AgentContext:
    """`on_step`: 스텝이 끝나는 즉시 호출되는 선택적 콜백 — 실시간 콘솔 출력용
    (§ DEBUGGING.md 3.1). `agent/react.py` 는 여기서도 rich/console 을 모른다 —
    콜백 시그니처만 안다.
    """
    budget = Budget(max_rounds=max_rounds) if max_rounds else Budget()
    ctx = AgentContext(target=target, session=session, llm=llm, store=store, budget=budget)

    # Phase 1: BOOTSTRAP
    ctx.principals = bootstrap_principals(session, target)
    ctx.graph.add_entitlements(entitlement_module.from_self_created(ctx.principals))

    # Phase 2/3: RECON(passive, 응답 본문에서 발견되는 경로도 1홉만 추가로 훑음)
    # + ENTITLEMENT 확정 (정상 경로만)
    recon_result = mapper_module.run(session, target, ctx.principals, ctx.graph)
    ctx.semantic.add_facts(recon_result.facts)

    # Phase 4: BASELINE 사전 학습 (정상 파라미터로 실측) — §6 순서를 지킨다.
    # LLM 은 `learn_baseline` 액션으로 추가 op 도 배울 수 있지만, 설정된 op 들은
    # 시험 시작 전에 미리 확보해둔다.
    principals_with_account = [p for p in ctx.principals if p.account_number]
    if len(principals_with_account) >= 2:
        payer, payee = principals_with_account[0], principals_with_account[1]
        for op_cfg in target.baseline_ops():
            ctx.baselines.learn(session, target, op_cfg, payer, counterparty=payee)

    ctx.templates = candidates_module.load_templates()
    ctx._prev_edge_count = ctx.graph.edge_count()

    system_prompt = prompts.build_react_system(ctx.templates)

    # Phase 5~N: ReAct 순환
    # ★ 최상위 try/except — 처리되지 않은 예외(우리 코드의 버그)가 나면 조용히
    # 죽지 않고 로그를 남기고 manifest 를 마무리한 뒤 다시 던진다. 이게 없으면
    # 크래시 시 store.finalize() 가 안 불려 manifest.finished_at 이 계속
    # null 로 남는다(§ DEBUGGING.md 3.4-b).
    try:
        while True:
            stop, reason = termination.should_stop(ctx)
            if stop:
                _finalize(store, reason, llm.usage)
                return ctx
            if ctx._finish_requested is not None:
                _finalize(store, ctx._finish_requested, llm.usage)
                return ctx

            ctx.round += 1
            edge_count_before = ctx.graph.edge_count()

            observation_text = _build_observation_text(ctx)
            user_prompt = prompts.build_react_user(observation_text)

            try:
                results = llm.complete_json(system_prompt, user_prompt, ReActStep, temperature=0.3)
            except LLMError as exc:
                # ★ API 자체의 실패(네트워크·레이트리밋·타임아웃) — 우리 코드의
                # 버그와 구분한다(§ DEBUGGING.md 3.4-a). 이번 스텝만 실패
                # 처리하고 계속 진행한다 — LLMError 가 아닌 예외는 여기서 안
                # 잡는다, 위 최상위 try 로 전파되어 조용히 묻히지 않는다.
                logger.warning("LLM 호출 실패 (step=%d): %s", ctx.round, exc)
                results = []

            if not results:
                observation = "LLM 이 유효한 JSON 액션을 내지 못함 (API 실패 또는 파싱 실패) — 다시 시도하라"
                step = Step(ctx.round, "", "", {}, observation)
            else:
                react_step = results[0]
                observation = _dispatch(react_step, ctx)
                step = Step(ctx.round, react_step.thought, react_step.action, react_step.action_input, observation)

            logger.info("step=%d action=%s", step.step_no, step.action or "(없음)")
            ctx.steps.append(step)
            store.append(
                "steps",
                {
                    "step": step.step_no,
                    "thought": step.thought,
                    "action": step.action,
                    "action_input": step.action_input,
                    "observation": step.observation,
                },
            )
            if on_step is not None:
                on_step(step)

            if ctx.graph.edge_count() > edge_count_before:
                ctx._steps_without_growth = 0
            else:
                ctx._steps_without_growth += 1
            ctx._prev_edge_count = edge_count_before

            store.snapshot_round(ctx.round)
    except Exception as exc:
        logger.exception("ReAct 루프가 처리되지 않은 예외로 중단됨 (step=%d)", ctx.round)
        _finalize(store, f"error: {exc.__class__.__name__}", llm.usage)
        raise
