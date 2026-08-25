"""ReAct 루프 — LLM 이 매 스텝 Thought → Action 하나를 내고, 코드가 그 Action 을
실행+판정해서 Observation 으로 돌려준다. 다음 스텝은 그 Observation 을 보고 또
LLM 이 정한다.

★ 여전히 LLM 은 액션만 고른다. Observation(오라클 판정 포함)은 항상 코드가
만든다 — `oracles/verifier.py` 는 순수 함수 그대로다. `propose_candidate` 액션의
`invariants`(오라클 목록)도 LLM 이 정하지 않는다 — attack_class 로부터
templates/attacks.yaml 에서 자동으로 끌어온다(§설계원칙 1, 3).
"""
from __future__ import annotations

import json
import secrets
from dataclasses import dataclass, field
from typing import Any

from finredteam.agent import strategy as strategy_module
from finredteam.agent import termination
from finredteam.agent.budget import Budget
from finredteam.knowledge.baseline import BaselineRegistry
from finredteam.knowledge.graph import KnowledgeGraph
from finredteam.llm.base import LLMClient, Usage
from finredteam.memory.episodic import EpisodicMemory
from finredteam.memory.semantic import SemanticMemory
from finredteam.memory.summarizer import summarize
from finredteam.memory.working import WorkingMemory
from finredteam.models.core import Principal, Verdict
from finredteam.models.finding import CandidateFinding, Finding
from finredteam.models.knowledge import Lesson
from finredteam.oracles import verifier as oracle_verifier
from finredteam.planning import candidates as candidates_module
from finredteam.planning import planner as planner_module
from finredteam.planning import prioritizer
from finredteam.planning import prompts
from finredteam.planning.actions import (
    FinishInput,
    LearnBaselineInput,
    ProposeCandidateInput,
    ReActStep,
    RecallMemoryInput,
    parse_action_input,
)
from finredteam.planning.candidates import AttackTemplate
from finredteam.probing import executor as executor_module
from finredteam.probing.session import Session
from finredteam.recon import entitlement as entitlement_module
from finredteam.recon import mapper as mapper_module
from finredteam.storage.run_store import RunStore
from finredteam.targets.loader import TargetConfig

_TARGET_UNHEALTHY_THRESHOLD = 5
_SATURATED_STEPS_WITHOUT_GROWTH = 6
_CONVERGED_STEPS_WITHOUT_GROWTH = 4


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

    round: int = 0  # ReAct 스텝 카운터. termination.py/budget.py 가 참조하는 이름을 그대로 쓴다.
    steps: list[Step] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    review_queue: list[Finding] = field(default_factory=list)

    _prev_edge_count: int = 0
    _steps_without_growth: int = 0
    _consecutive_request_errors: int = 0
    _finish_requested: str | None = None

    # ── agent/strategy.py::suggest_nudge 가 요구하는 신호 ─────────────
    def has_pending_race_trials(self) -> bool:
        return False

    def graph_grew_significantly(self) -> bool:
        return self.graph.grew_significantly(self._prev_edge_count)

    def recent_violations(self) -> bool:
        return any(f.verdict == Verdict.VIOLATION for f in self.findings[-5:])

    def successful_classes(self) -> list[str]:
        classes = sorted({f.candidate.attack_class for f in self.findings if f.verdict == Verdict.VIOLATION})
        return classes or list(self.templates.keys())

    def consecutive_failures(self) -> int:
        return self.episodic.consecutive_failures()

    def least_tried_classes(self) -> list[str]:
        tried = self.episodic.tried_attack_classes()
        untried = sorted(set(self.templates.keys()) - tried)
        return untried or sorted(self.templates.keys())

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
        all_tried = self.templates and self.episodic.tried_attack_classes() >= set(self.templates.keys())
        return bool(all_tried) and self._steps_without_growth >= _CONVERGED_STEPS_WITHOUT_GROWTH


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


def _handle_propose_candidate(input_: ProposeCandidateInput, ctx: AgentContext) -> str:
    skeleton = candidates_module.resolve_skeleton(
        input_.attack_class,
        input_.principal_id,
        input_.target,
        ctx.principals,
        ctx.graph,
        ctx.target,
        ctx.templates,
    )
    if isinstance(skeleton, str):
        return skeleton  # 검증 실패 사유를 그대로 Observation 으로 — LLM 이 다음 스텝에서 고친다

    candidate = planner_module.build_candidate(skeleton, input_.params, "", ctx.baselines)

    if prioritizer.already_tried_and_failed(candidate, ctx.episodic):
        return "이미 정확히 같은 조합(attack_class·주체·경로·파라미터)을 시도했고 VIOLATION 이 없었다 — 다른 파라미터나 대상을 시도하라"

    principal = next(p for p in ctx.principals if p.id == candidate.principal_id)
    watch_accounts = _watch_accounts_for(candidate, principal)

    result = executor_module.run(ctx.session, ctx.target, candidate, principal, watch_accounts, db=ctx.store.db)
    if all(code == 0 for code in result.status_codes):
        ctx._consecutive_request_errors += 1
    else:
        ctx._consecutive_request_errors = 0

    finding = oracle_verifier.verify(candidate, result, graph=ctx.graph)
    ctx.store.append("findings", finding.model_dump(mode="json"))

    for account in watch_accounts:
        ctx.graph.apply_observed(candidate.principal_id, f"account:{account}", "read")

    lesson = Lesson(
        candidate_summary=candidate.id,
        outcome=finding.verdict.value,
        insight=finding.reason,
        attack_class=candidate.attack_class,
    )
    ctx.episodic.record(candidate, finding, lesson)

    if finding.verdict == Verdict.VIOLATION:
        ctx.findings.append(finding)
    elif finding.verdict in (Verdict.ANOMALY, Verdict.UNKNOWN):
        ctx.review_queue.append(finding)

    return f"{finding.verdict.value} ({finding.invariant}): {finding.reason}"


def _dispatch(step: ReActStep, ctx: AgentContext) -> str:
    parsed = parse_action_input(step.action, step.action_input)
    if isinstance(parsed, str):
        return f"입력 오류: {parsed}"

    if step.action == "observe_graph":
        menu = candidates_module.graph_menu(ctx.principals, ctx.graph)
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
        return f"{parsed.op} baseline 학습 완료 (ref={record.ref_id}) — 이제 이 연산에 대한 후보는 baseline_observed 로 판정 가능"

    if step.action == "propose_candidate":
        assert isinstance(parsed, ProposeCandidateInput)
        return _handle_propose_candidate(parsed, ctx)

    if step.action == "finish":
        assert isinstance(parsed, FinishInput)
        ctx._finish_requested = parsed.reason or "llm_requested"
        return "종료 요청 접수"

    return f"알 수 없는 액션: {step.action!r}"


_SCRATCHPAD_WINDOW = 6


def _format_scratchpad(steps: list[Step], window: int = _SCRATCHPAD_WINDOW) -> str:
    """최근 스텝들의 Thought/Action/Observation 을 그대로 보여준다.

    ReAct 의 핵심은 이 누적 컨텍스트다 — 직전 1스텝만 보여주면 "방금 관찰한
    그래프 메뉴"가 두 스텝 뒤엔 사라져서 LLM 이 같은 걸 또 확인하러 간다.
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
        f"지금까지의 진행 기록:\n{_format_scratchpad(ctx.steps)}",
    ]
    nudge = strategy_module.suggest_nudge(ctx)
    if nudge:
        lines.append(nudge)
    lines.append(f"이전 시도 요약:\n{summarize(ctx.semantic, ctx.episodic)}")
    return "\n\n".join(lines)


def run(
    target: TargetConfig, session: Session, llm: LLMClient, store: RunStore, *, max_rounds: int | None = None
) -> AgentContext:
    budget = Budget(max_rounds=max_rounds) if max_rounds else Budget()
    ctx = AgentContext(target=target, session=session, llm=llm, store=store, budget=budget)

    # Phase 1: BOOTSTRAP
    ctx.principals = bootstrap_principals(session, target)
    ctx.graph.add_entitlements(entitlement_module.from_self_created(ctx.principals))

    # Phase 2/3: RECON(passive) + ENTITLEMENT 확정 (정상 경로만)
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
    while True:
        stop, reason = termination.should_stop(ctx)
        if stop:
            store.finalize(reason, usage=llm.usage)
            return ctx
        if ctx._finish_requested is not None:
            store.finalize(ctx._finish_requested, usage=llm.usage)
            return ctx

        ctx.round += 1
        edge_count_before = ctx.graph.edge_count()

        observation_text = _build_observation_text(ctx)
        user_prompt = prompts.build_react_user(observation_text)
        results = llm.complete_json(system_prompt, user_prompt, ReActStep, temperature=0.3)

        if not results:
            observation = "LLM 이 유효한 JSON 액션을 내지 못함 (파싱 실패 또는 빈 응답) — 다시 시도하라"
            step = Step(ctx.round, "", "", {}, observation)
        else:
            react_step = results[0]
            observation = _dispatch(react_step, ctx)
            step = Step(ctx.round, react_step.thought, react_step.action, react_step.action_input, observation)

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

        if ctx.graph.edge_count() > edge_count_before:
            ctx._steps_without_growth = 0
        else:
            ctx._steps_without_growth += 1
        ctx._prev_edge_count = edge_count_before

        store.snapshot_round(ctx.round)
