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
import time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

from finredteam.agent import strategy as strategy_module
from finredteam.agent import termination
from finredteam.agent.budget import Budget
from finredteam.config import MAX_PROBES, RACE_TRIALS
from finredteam.engagement.cleanup import CreatedResource
from finredteam.engagement.cleanup import run_cleanup as run_cleanup_phase
from finredteam.engagement.errors import PolicyViolation
from finredteam.engagement.policy import CompiledPolicy
from finredteam.engagement.schema import EngagementCredential
from finredteam.knowledge.baseline import BaselineRecord, BaselineRegistry
from finredteam.knowledge.graph import KnowledgeGraph
from finredteam.llm.base import LLMClient, LLMError, Usage
from finredteam.memory.episodic import EpisodicMemory
from finredteam.memory.semantic import SemanticMemory
from finredteam.memory.summarizer import summarize
from finredteam.memory.working import WorkingMemory
from finredteam.models.core import Principal, Verdict
from finredteam.money import Money
from finredteam.models.finding import CandidateFinding, ExecutionResult, Finding
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
    RunProbeInput,
    parse_action_input,
)
from finredteam.planning.candidates import AttackTemplate, CandidateSkeleton
from finredteam.probing import executor as executor_module
from finredteam.probing.finance import auth_exposure, burst, card_exposure, card_fund, charge, loan, privesc
from finredteam.probing.generic import api as generic_api
from finredteam.probing.generic import web as generic_web
from finredteam.probing.session import Session, SessionError
from finredteam.recon import auth_discovery
from finredteam.recon import entitlement as entitlement_module
from finredteam.recon import fingerprint as fingerprint_module
from finredteam.recon import mapper as mapper_module
from finredteam.recon import operation_discovery
from finredteam.reporting.transcript import write_transcript
from finredteam.storage.run_store import RunStore
from finredteam.targets.loader import TargetConfig

logger = logging.getLogger(__name__)

_TARGET_UNHEALTHY_THRESHOLD = 5
_SATURATED_STEPS_WITHOUT_GROWTH = 6
_CONVERGED_STEPS_WITHOUT_GROWTH = 4
_LLM_UNAVAILABLE_THRESHOLD = 5  # target_unhealthy()와 같은 자리 — "LLM이 계속 실패" 전용 신호
_LLM_BACKOFF_CAP_SECONDS = 30.0
_LATENCY_BASELINE_SAMPLES = 3  # latency_degradation 기준선으로 쓸 초기 표본 수

# external_side_effect best-effort 힌트(engagement §12 한계, 확정 판정 아님) — 한국어+영어.
_EXTERNAL_SIDE_EFFECT_MARKERS = (
    "email sent", "notification sent", "sms sent", "webhook",
    "이메일이 발송", "알림이 발송", "문자가 발송",
)


def _mentions_external_side_effect(texts: list[str]) -> bool:
    return any(marker in t.lower() for t in texts for marker in _EXTERNAL_SIDE_EFFECT_MARKERS)


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
    policy: CompiledPolicy

    principals: list[Principal] = field(default_factory=list)
    graph: KnowledgeGraph = field(default_factory=KnowledgeGraph)
    working: WorkingMemory = field(default_factory=WorkingMemory)
    semantic: SemanticMemory = field(default_factory=SemanticMemory)
    episodic: EpisodicMemory = field(default_factory=EpisodicMemory)
    baselines: BaselineRegistry = field(default_factory=BaselineRegistry)
    templates: dict[str, AttackTemplate] = field(default_factory=dict)
    pending_hypotheses: dict[str, Hypothesis] = field(default_factory=dict)
    created_resources: list[CreatedResource] = field(default_factory=list)
    # ★ Black-box Auth Discovery(recon/auth_discovery.py)가 실패해서 사람이 작성한
    # 어댑터 YAML(LEGACY_BOOTSTRAP)로 폴백한 부분 — 절대 숨기지 않는다(§11).
    # 비어 있으면 이 실행의 인증 부분은 완전한 autonomous black-box 였다는 뜻.
    legacy_bootstrap_used: list[str] = field(default_factory=list)
    # ★ Operation Discovery(recon/operation_discovery.py)가 찾은 송금형 operation —
    # operation 이름("transfer") -> 발견된 endpoint/method/필드. 있으면
    # planning/candidates.py::resolve_skeleton 이 정적 baseline_ops 대신 이걸 쓴다.
    discovered_operations: dict[str, operation_discovery.DiscoveredOperation] = field(default_factory=dict)

    round: int = 0  # ReAct 스텝 카운터. termination.py/budget.py 가 참조하는 이름을 그대로 쓴다.
    steps: list[Step] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    review_queue: list[Finding] = field(default_factory=list)

    _prev_edge_count: int = 0
    _steps_without_growth: int = 0
    _consecutive_request_errors: int = 0
    _consecutive_5xx: int = 0
    _consecutive_llm_errors: int = 0
    _finish_requested: str | None = None
    _hypothesis_counter: int = 0
    _discover_calls: int = 0
    _latency_baseline_samples: list[float] = field(default_factory=list)
    _consecutive_latency_degraded: int = 0
    _critical_sensitive_hits: int = 0
    _external_side_effect_hits: int = 0  # 로깅/향후 확장용 — policy_stop_reason() 은 안 봄

    # ── agent/strategy.py::suggest_nudge 가 요구하는 신호 ─────────────
    def has_pending_race_trials(self) -> bool:
        return False

    def graph_grew_significantly(self) -> bool:
        return self.graph.grew_significantly(self._prev_edge_count)

    def baseline_learned_but_never_tried(self) -> list[str]:
        """★ 실측 버그 대응: BOOTSTRAP 이 `target.baseline_ops()`(예: transfer)를
        미리 다 학습해두는데도, read_balance 처럼 계속 VIOLATION 이 나오는
        operation 에만 머물러 baseline까지 있는 다른 operation(transfer)을 한
        번도 propose_hypothesis 로 안 내는 사례가 있었다(성과가 계속 나는 쪽에만
        머무르니 §14 "2연속 실패 시 전환" 넛지도 안 켜졌다). 성공/실패와 무관하게
        "이미 준비됐는데 안 써먹은" operation을 알려준다."""
        tried = self.episodic.tried_operations()
        return sorted(
            op for op in candidates_module.supported_operations() if self.baselines.has(op) and op not in tried
        )

    def ready_for_hypothesis_but_none_proposed(self) -> bool:
        """★ 실측 버그 대응: BOOTSTRAP 직후 이미 entitled_own(계좌)이 채워져
        있는데도 LLM이 discover_surface/observe_graph 만 반복하다 saturated 로
        끝나는 사례가 있었다 — propose_hypothesis 를 한 번도 안 낸 상태에서
        가설을 낼 재료(계좌)가 이미 있으면 매 스텝 알려준다."""
        return self._hypothesis_counter == 0 and bool(self.graph.entitled_edges())

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

    def untried_probes(self) -> list[str]:
        """run_probe 로 아직 한 번도 안 돌린 프로브 종류를 돌려준다 — 하나만 하고
        멈추지 않게, 남은 종류를 계속 콕 집어 유도하기 위해서다."""
        probe_ops = {
            "sensitive_exposure": {"register_exposure", "forgot_password_exposure", "card_exposure"},
            "privilege_escalation": {"mass_assignment_privesc"},
            "card_fund": {"card_fund"},
            "sqli_scan": {"sqli_check_balance", "sqli_transactions"},
            "jwt_forge": {"jwt_weak_secret"},
            "charge_idempotency": {"merchant_charge"},
            "race_transfer": {"race_transfer"},
            "loan_limit": {"request_loan"},
        }
        tried = self.episodic.tried_operations()
        return [name for name, ops in probe_ops.items() if not (ops & tried)]

    def layer_untouched(self, layer: str) -> bool:
        return not any(ep.candidate.layer == layer for ep in self.episodic.all())

    # ── agent/termination.py 가 요구하는 신호 ───────────────────────
    @property
    def llm_usage(self) -> Usage:
        return self.llm.usage

    def target_unhealthy(self) -> bool:
        return self._consecutive_request_errors >= _TARGET_UNHEALTHY_THRESHOLD

    def llm_unavailable(self) -> bool:
        """LLM 호출이 연속으로 실패(레이트리밋·타임아웃·인증 등, §DEBUGGING.md 3.4-a)
        하는 경우 — `target_unhealthy()`는 대상(target) 요청 실패를 보는 신호라
        구분한다. 이게 없으면 "saturated"(그래프 정체)로 뭉뚱그려져서, 실제로는
        LLM이 한 번도 응답하지 못했는데 원인이 안 보이는 문제가 있었다."""
        return self._consecutive_llm_errors >= _LLM_UNAVAILABLE_THRESHOLD

    # ── Engagement Stop Condition (engagement §12) — LLM이 계속하자고 해도 우선한다 ──
    def policy_stop_reason(self) -> str | None:
        if self.policy.stop_repeated_5xx and self._consecutive_5xx >= self.policy.stop_repeated_5xx_threshold:
            return f"policy_stop:repeated_5xx(>={self.policy.stop_repeated_5xx_threshold})"
        if (
            self.policy.stop_unexpected_sensitive_data
            and self._critical_sensitive_hits >= self.policy.stop_unexpected_sensitive_data_threshold
        ):
            return f"policy_stop:unexpected_sensitive_data(>={self.policy.stop_unexpected_sensitive_data_threshold})"
        if (
            self.policy.stop_latency_degradation
            and self._consecutive_latency_degraded >= self.policy.stop_latency_degradation_consecutive_threshold
        ):
            return (
                "policy_stop:latency_degradation("
                f">={self.policy.stop_latency_degradation_consecutive_threshold}x"
                f"{self.policy.stop_latency_degradation_multiplier}baseline)"
            )
        return None

    def saturated(self) -> bool:
        return self._steps_without_growth >= _SATURATED_STEPS_WITHOUT_GROWTH

    def converged(self) -> bool:
        all_ops = set(candidates_module.supported_operations())
        all_tried = bool(all_ops) and self.episodic.tried_operations() >= all_ops
        return all_tried and self._steps_without_growth >= _CONVERGED_STEPS_WITHOUT_GROWTH


# 대시보드류 "로그인 후 정상적으로 보이는 첫 페이지"를 찾기 위한 후보 — 사람이
# *이* 대상을 조사해서 적어둔 값이 아니라, 웹앱 흔한 관례(§ Human pre-analysis
# 제거, 기존 discovery.seed_paths 의 "정상적으로 앱을 쓰면 자연히 보이는 최소한"
# 취지와 동일)다. seed_paths 에 이미 있으면 그것도 같이 시도한다(있어도 없어도
# 동작).
_AUTHED_HOME_CANDIDATES = ("/dashboard", "/home", "/account", "/profile", "/portal")


def discover_and_learn_money_operation(
    session: Session,
    target: TargetConfig,
    payer: Principal,
    payee: Principal,
    baselines: BaselineRegistry,
) -> operation_discovery.DiscoveredOperation | None:
    """로그인 후 정상적으로 보이는 페이지(대시보드류)에서 송금형 operation을
    찾고, 소액(1) 정상 실행 한 번으로 diff 기반 baseline까지 학습한다 — 사람이
    적어둔 `baseline_ops`(LEGACY_BOOTSTRAP) 없이도 동작한다. 실패하면(폼을 못
    찾음, 잔액 관측 실패 등) `None` — 예외 없음, 호출부가 정적 config 로 폴백한다.

    ★ 한계: payer 쪽 델타만 실측한다(payee 쪽까지 관측하려면 별도 로그인 페이지
    재요청이 더 필요해 이번 패스에서는 범위 밖) — payer 델타만으로도
    `ExpectedDelta.provenance="baseline_observed"`는 유효하다, 완전하지 않을 뿐이다.
    """
    # ★ _AUTHED_HOME_CANDIDATES 를 seed_paths 보다 먼저 본다 — `/`/`/login`/
    # `/register` 처럼 seed_paths 에 흔히 있는 "공개" 경로는 인증 여부와
    # 무관하게 200을 내서, 먼저 시도하면 그게 "홈"으로 잘못 뽑힌다(실측
    # 버그: `/` 가 먼저 걸려 대시보드를 아예 못 봄).
    disc = target.discovery_config() if isinstance(target.discovery_config(), dict) else {}
    seed_hint = disc.get("seed_paths", []) if isinstance(disc, dict) else []
    candidate_paths = list(dict.fromkeys(list(_AUTHED_HOME_CANDIDATES) + list(seed_hint)))[:MAX_PROBES]

    home_path: str | None = None
    home_html: str | None = None
    pages: dict[str, str] = {}
    for path in candidate_paths:
        try:
            ex = session.authed_request("GET", path, payer)
        except SessionError:
            continue
        if ex.status_code >= 400:
            continue
        pages[path] = ex.response_text
        home_path, home_html = path, ex.response_text
        break

    if home_html is None or home_path is None:
        return None

    for js_path in sorted(fingerprint_module.extract_discovered_paths(home_html))[:MAX_PROBES]:
        if not js_path.endswith(".js") or js_path in pages:
            continue
        try:
            js_ex = session.authed_request("GET", js_path, payer)
        except SessionError:
            continue
        if js_ex.status_code < 400:
            pages[js_path] = js_ex.response_text

    op = operation_discovery.discover_money_operation(pages)
    if op is None:
        return None

    before_balance = operation_discovery.discover_self_balance(home_html)
    if before_balance is None or not payee.account_number or not payer.account_number:
        return None

    params = {op.amount_field: "1", op.recipient_field: payee.account_number}
    try:
        ex = session.authed_request(op.method, op.endpoint, payer, json_body=params)
    except SessionError:
        return None
    if ex.status_code >= 400:
        return None

    try:
        after_ex = session.authed_request("GET", home_path, payer)
    except SessionError:
        return None
    after_balance = operation_discovery.discover_self_balance(after_ex.response_text)
    if after_balance is None:
        return None

    delta = after_balance - before_balance
    if delta == 0:
        return None  # 실측 델타가 없으면 baseline 으로 못 쓴다(§6 — 판정 근거는 항상 실측)

    baselines.register(
        BaselineRecord(
            ref_id="bl_transfer_discovered",
            op="transfer",
            path=op.endpoint,
            principal_id=payer.id,
            deltas={payer.account_number: Money(amount=delta, currency=target.adapter.default_currency)},
            roles={payer.account_number: "payer"},
            normal_params=params,
        )
    )
    return op


def pre_crawl_for_auth_discovery(session: Session) -> tuple[dict[str, str], str | None]:
    """BOOTSTRAP 전에 비인증 상태로 로그인/회원가입 폼을 찾기 위한 가벼운 사전
    크롤 — `/` 하나에서 시작해 응답 본문에서 발견된 경로를 1홉만 더 본다(기존
    `recon/fingerprint.py::extract_discovered_paths` 재사용, `recon/mapper.py`와
    같은 무한크롤 방지 철학, `MAX_PROBES` 상한). 실측 확인(2026-08-26): `/` 하나
    만으로도 `/login`/`/register` 링크가 응답 본문에 있어 발견된다 — seed_paths
    설정에 의존하지 않는다(Human pre-analysis 제거 취지).

    두 번째 반환값은 인증이 필요한 것으로 관측된(401/403) 경로 하나 —
    `recon/auth_discovery.py`가 발견한 토큰의 헤더 스킴을 검증할 때 쓴다.
    """
    pages: dict[str, str] = {}
    seen: set[str] = set()
    protected_path: str | None = None

    def _fetch(path: str) -> set[str]:
        nonlocal protected_path
        if path in seen:
            return set()
        seen.add(path)
        try:
            ex = session.anonymous_request("GET", path)
        except SessionError:
            return set()
        pages[path] = ex.response_text
        if protected_path is None and ex.status_code in (401, 403):
            protected_path = path
        return fingerprint_module.extract_discovered_paths(ex.response_text) - seen

    discovered = _fetch("/")
    for path in sorted(discovered)[:MAX_PROBES]:
        _fetch(path)

    return pages, protected_path


def _login_via_discovery_or_legacy(
    session: Session,
    principal_id: str,
    cred: EngagementCredential,
    discovery_pages: dict[str, str] | None,
    protected_path: str | None,
    legacy_bootstrap_used: set[str],
) -> Principal:
    if discovery_pages:
        discovered = auth_discovery.discover_login(session, discovery_pages, cred, protected_path=protected_path)
        if discovered is not None:
            # ★ 이후 모든 authed_request() 가 이 스킴을 쓰게 한다 — 안 하면
            # 정적 adapter.auth.token_header(없을 수도 있음, minimal_target_
            # config) 로 조용히 폴백하다 크래시하거나 틀린 헤더로 나간다.
            session.set_discovered_auth_header(discovered.header_name, discovered.header_value_template)
            return Principal(
                id=principal_id,
                username=cred.username,
                password=cred.password,
                token=discovered.token,
                account_number=discovered.account_number,
            )
    legacy_bootstrap_used.add("login")
    return session.login(Principal(id=principal_id, username=cred.username, password=cred.password))


def _register_and_login_via_discovery_or_legacy(
    session: Session,
    principal_id: str,
    cred: EngagementCredential,
    discovery_pages: dict[str, str] | None,
    protected_path: str | None,
    legacy_bootstrap_used: set[str],
) -> Principal:
    if discovery_pages and auth_discovery.discover_register(session, discovery_pages, cred):
        discovered = auth_discovery.discover_login(session, discovery_pages, cred, protected_path=protected_path)
        if discovered is not None:
            session.set_discovered_auth_header(discovered.header_name, discovered.header_value_template)
            return Principal(
                id=principal_id,
                username=cred.username,
                password=cred.password,
                token=discovered.token,
                account_number=discovered.account_number,
            )
        # 등록은 discovery 로 성공했는데 로그인 discovery 만 실패했다 — 다시
        # session.bootstrap() 을 부르면 같은 계정을 또 등록하려다 "이미 존재함"
        # 에러가 난다. legacy 로그인만 폴백으로 쓴다(재등록 금지).
        legacy_bootstrap_used.add("login")
        return session.login(Principal(id=principal_id, username=cred.username, password=cred.password))
    legacy_bootstrap_used.update({"register", "login"})
    return session.bootstrap(principal_id, cred.username, cred.password)


def bootstrap_principals(
    session: Session,
    target: TargetConfig,
    credentials: list[EngagementCredential] | None = None,
    *,
    allow_account_creation: bool,
    discovery_pages: dict[str, str] | None = None,
    protected_path: str | None = None,
) -> tuple[list[Principal], list[str]]:
    """`target.settings.principals` 순서대로 `credentials`를 매칭한다 — 있으면
    등록 없이 로그인만(§engagement 3: "Credentials → 사용자 제공 가능"),
    없으면 `allow_account_creation` 일 때만 새로 등록한다. 어느 쪽도 안 되는
    principal 이 하나라도 있으면 그 자리에서 실패한다(LLM에게 절대 안 맡기는
    계정생성 판단 — engagement §14).

    `discovery_pages`(BOOTSTRAP 전 비인증 사전 크롤 결과, `run()` 참고)가 있으면
    먼저 Black-box Auth Discovery(recon/auth_discovery.py)로 로그인/회원가입
    endpoint·필드·인증 헤더를 직접 찾아서 쓴다. 실패하면 `targets/configs/
    <name>.yaml`의 정적 `adapter.auth.*`(LEGACY_BOOTSTRAP)로 폴백한다 — 이걸
    조용히 넘어가지 않고 두 번째 반환값(`legacy_bootstrap_used`, 예:
    `["login"]`)으로 정직하게 알린다(§11, 절대 숨기지 않는다).
    """
    credentials = credentials or []
    principals: list[Principal] = []
    missing_without_creation: list[str] = []
    legacy_bootstrap_used: set[str] = set()

    for i, principal_id in enumerate(target.settings.principals):
        if i < len(credentials):
            cred = credentials[i]
            principal = _login_via_discovery_or_legacy(
                session, principal_id, cred, discovery_pages, protected_path, legacy_bootstrap_used
            )
            principal.extra["bootstrap_source"] = "provisioned"
        elif allow_account_creation:
            cred = EngagementCredential(
                username=f"{principal_id}_{secrets.token_hex(3)}_{secrets.token_hex(3)}",
                password=f"Rt!{secrets.token_urlsafe(8)}",
            )
            principal = _register_and_login_via_discovery_or_legacy(
                session, principal_id, cred, discovery_pages, protected_path, legacy_bootstrap_used
            )
            principal.extra["bootstrap_source"] = "self_created"
        else:
            missing_without_creation.append(principal_id)
            continue
        principals.append(principal)

    if missing_without_creation:
        raise PolicyViolation(
            "ACCOUNT_CREATION_FORBIDDEN",
            f"주체 {missing_without_creation} 에 대한 사전 제공 계정이 없고 "
            "Engagement 정책이 계정 생성을 허용하지 않음",
        )
    return principals, sorted(legacy_bootstrap_used)


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
        input_.operation, input_.principal_id, input_.target, ctx.principals, ctx.graph, ctx.target,
        ctx.discovered_operations,
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

    # ★ Engagement Hard Constraint 사전 검사 — 실제 요청을 보내기 전에 막는다.
    # `probing/session.py::Session._request()` 가 같은 걸 다시 검사하므로(방어
    # 이중화) 여기가 빠지거나 우회돼도 네트워크로는 못 나간다 — 다만 여기서
    # 막으면 LLM에게 "왜" 막혔는지 바로 알려주고 실행 자체를 안 하게 된다.
    if not ctx.policy.is_method_allowed(candidate.method):
        logger.warning("정책 위반으로 실행 차단 (step=%d): %s %s", ctx.round, candidate.method, candidate.path)
        return (
            f"정책 위반으로 차단됨(STATE_CHANGE_FORBIDDEN): {candidate.method} {candidate.path} 는 "
            "이 Engagement 정책상 허용되지 않음 — 실제 요청을 보내지 않았음"
        )

    amount_raw = candidate.params.get("amount")
    if amount_raw is not None:
        try:
            amount = Decimal(str(amount_raw))
        except InvalidOperation:
            amount = None
        if amount is not None:
            try:
                ctx.policy.check_amount(amount)
            except PolicyViolation as exc:
                logger.warning("정책 위반으로 실행 차단 (step=%d): %s", ctx.round, exc)
                return f"정책 위반으로 차단됨({exc.rule}): {exc.detail} — 실제 요청을 보내지 않았음"

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

    # Engagement Stop Condition: repeated_5xx (engagement §12) — target_unhealthy 와
    # 별개 신호다. target_unhealthy 는 "요청 자체가 실패"(code==0), 이건 "대상이 5xx를
    # 반환"이다 — 대상이 살아있고 응답은 하는데 계속 서버 에러를 내는 경우를 잡는다.
    if result.status_codes and all(500 <= code < 600 for code in result.status_codes):
        ctx._consecutive_5xx += 1
    else:
        ctx._consecutive_5xx = 0

    # Engagement Stop Condition: unexpected_sensitive_data — CRITICAL 등급 필드
    # (password/cvv/card_number/... knowledge/sensitive.py)만 이상 신호로 본다.
    # HIGH(balance/token/account_number 등)는 정상 인증 응답에도 흔해 오탐 유발.
    if any(level == "CRITICAL" for level in result.sensitive_levels):
        ctx._critical_sensitive_hits += 1

    # Engagement Stop Condition: latency_degradation — 단발성 스파이크 오탐을
    # 피하려고 baseline(첫 N개 표본 평균) 대비 "연속" 초과만 카운트한다.
    for lat in result.latencies_ms:
        if len(ctx._latency_baseline_samples) < _LATENCY_BASELINE_SAMPLES:
            ctx._latency_baseline_samples.append(lat)
            ctx._consecutive_latency_degraded = 0
            continue
        baseline_avg = sum(ctx._latency_baseline_samples) / len(ctx._latency_baseline_samples)
        if baseline_avg > 0 and lat > baseline_avg * ctx.policy.stop_latency_degradation_multiplier:
            ctx._consecutive_latency_degraded += 1
        else:
            ctx._consecutive_latency_degraded = 0

    # ★ external_side_effect 는 Hard Stop 이 아니다(engagement §12 한계) — 실제
    # out-of-scope 요청은 이미 CompiledPolicy.check_request 가 원천 차단하므로,
    # 여기서 볼 수 있는 건 "대상 백엔드가 제3자에게 뭔가를 했을 수도 있다"는
    # 응답 본문 키워드뿐이다. best-effort 참고 신호로만 Observation 에 덧붙인다.
    external_side_effect_note = ""
    if _mentions_external_side_effect(result.response_texts):
        ctx._external_side_effect_hits += 1
        external_side_effect_note = (
            "\n[참고] 응답에서 외부 부작용(이메일/webhook 등) 가능성 키워드 감지 — "
            "best-effort 추정이며 확정된 판정이 아님."
        )

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
        observation = "\n".join(f"{o.invariant}: {o.verdict.value} — {o.reason}" for o in noteworthy)
    else:
        passed = [o.invariant for o in outcomes if o.verdict == Verdict.PASS]
        observation = (
            f"이상 없음 (PASS: {passed})"
            if passed
            else "판정 가능한 오라클 없음 (전부 UNKNOWN) — 그래프 정보나 baseline 이 더 필요할 수 있음"
        )
    return observation + external_side_effect_note


def _record_probe_finding(ctx: AgentContext, candidate: CandidateFinding, result: ExecutionResult) -> str:
    """특수 프로브 결과를 execute_experiment 와 같은 방식으로 판정·기록한다.
    오라클 전체 집합이 돌고(등록 오라클이 자동 판정), Finding 을 저장·기억에 남긴다."""
    outcomes = oracle_verifier.run_all_outcomes(candidate, result, ctx.graph)
    finding = oracle_verifier.verify_from_outcomes(candidate, result, outcomes)
    ctx.store.append("findings", finding.model_dump(mode="json"))
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
    return f"{finding.invariant or '-'}: {finding.verdict.value} — {finding.reason}"


def _record_generic_outcome(ctx: AgentContext, operation: str, method: str, path: str, outcome) -> str:
    """Generic 프로브(SQLi/JWT 등)의 OracleOutcome 을 Finding 으로 저장한다.

    Generic 오라클은 registry(INV-*) 가 아니라 프로브가 직접 판정을 낸다 — 그래서
    run_all 을 거치지 않고 outcome 을 그대로 Finding 으로 감싼다. path 는 채점기가
    GT 와 맞출 수 있도록 payload 가 아닌 '정상 경로'(예: /check_balance/{계좌})로 남긴다."""
    candidate = CandidateFinding(
        id=f"{operation}-{secrets.token_hex(3)}", layer="generic",
        operation=operation, principal_id="anon", method=method, path=path,
    )
    result = ExecutionResult(candidate=candidate, status_codes=[200])
    finding = Finding(
        candidate=candidate, result=result, verdict=outcome.verdict,
        invariant=outcome.invariant, reason=outcome.reason,
    )
    ctx.store.append("findings", finding.model_dump(mode="json"))
    ctx.episodic.record(
        candidate, finding,
        Lesson(candidate_summary=candidate.id, outcome=finding.verdict.value,
               insight=finding.reason, operation=candidate.operation),
    )
    if finding.verdict == Verdict.VIOLATION:
        ctx.findings.append(finding)
    elif finding.verdict in (Verdict.ANOMALY, Verdict.UNKNOWN):
        ctx.review_queue.append(finding)
    return f"{outcome.invariant}: {outcome.verdict.value} — {outcome.reason}"


def _record_race_finding(ctx: AgentContext, candidate: CandidateFinding, summary: dict) -> str:
    """경쟁조건(INV-1C) 반복 시행 결과를 하나의 Finding 으로 종합·기록한다.

    확률적이라 여러 번 시행하고(§7 규칙 1), 판정 가능한 시행 중 한 번이라도 초과
    인출이 재현되면 VIOLATION(재현율 첨부), 판정 가능한 시행이 하나도 없으면
    (잔액 회복 실패 등) UNKNOWN, 전부 정상이면 PASS 로 종합한다. INV-1C 는 여러
    시행을 가로질러 판정하므로 execute_experiment 의 단일 result 경로가 아니라
    이 전용 종합 경로를 쓴다(burst.py 와 같은 이유)."""
    violations = summary.get("violations", 0)
    judged = summary.get("judged", 0)
    rate = summary.get("reproduction_rate", 0.0)
    if violations > 0:
        verdict = Verdict.VIOLATION
        reason = f"경쟁조건 재현 — 판정 {judged}회 중 {violations}회 초과 인출 확인(재현율 {rate})"
    elif judged == 0:
        verdict = Verdict.UNKNOWN
        reason = f"판정 가능한 시행 없음(스킵 {summary.get('skipped', 0)}회) — 잔액 관측·회복 실패 가능"
    else:
        verdict = Verdict.PASS
        reason = f"{judged}회 시행 모두 잔액 검사가 원자적으로 지켜짐(초과 인출 없음)"

    result = ExecutionResult(candidate=candidate, status_codes=[])
    finding = Finding(
        candidate=candidate, result=result, verdict=verdict, invariant="INV-1C", reason=reason
    )
    ctx.store.append("findings", finding.model_dump(mode="json"))
    ctx.episodic.record(
        candidate, finding,
        Lesson(candidate_summary=candidate.id, outcome=verdict.value, insight=reason, operation=candidate.operation),
    )
    if verdict == Verdict.VIOLATION:
        ctx.findings.append(finding)
    elif verdict in (Verdict.ANOMALY, Verdict.UNKNOWN):
        ctx.review_queue.append(finding)
    return f"INV-1C: {verdict.value} — {reason}"


def _handle_run_probe(input_: RunProbeInput, ctx: AgentContext) -> str:
    """계좌·송금 외 특수 취약점을 전용 프로브로 시험한다 — 셋업(계정/카드 생성)은
    코드가 자동으로 하고, 판정은 등록된 오라클이 낸다. 실패해도 예외 없이 에러
    문자열을 Observation 으로 돌려준다(§sad path)."""
    pairs: list[tuple[CandidateFinding, ExecutionResult]] = []
    owner = next((p for p in ctx.principals if p.account_number), None)

    if input_.probe == "sensitive_exposure":
        tag = secrets.token_hex(3)
        username = f"expo_{tag}"
        pairs.append(auth_exposure.run_register_password_echo_probe(ctx.session, username, "Rt!pass123"))
        pairs.append(auth_exposure.run_forgot_password_pin_probe(ctx.session, username))
        if owner is not None and ctx.session.create_virtual_card(owner) is not None:
            pairs.append(card_exposure.run_card_exposure_probe(ctx.session, owner))

    elif input_.probe == "privilege_escalation":
        pairs.append(privesc.run_admin_escalation_probe(ctx.session))

    elif input_.probe == "card_fund":
        if owner is None:
            return "card_fund 프로브: 카드 소유자로 쓸 계좌 있는 주체가 없음"
        card_id = ctx.session.create_virtual_card(owner, card_limit="9999999.00")
        if card_id is None:
            return "card_fund 프로브: 카드 생성 실패"
        pairs.append(
            card_fund.run_card_fund_manipulation(ctx.session, owner, card_id, usd_amount="100.00", exchange_rate="100")
        )

    elif input_.probe == "charge_idempotency":
        if owner is None:
            return "charge_idempotency 프로브: 카드 소유자로 쓸 계좌 있는 주체가 없음"
        card_id = ctx.session.create_virtual_card(owner, card_limit="9999999.00")
        if card_id is None:
            return "charge_idempotency 프로브: 카드 생성 실패"
        # 반복 결제가 잔액 부족으로 막히면 멱등성 여부를 관측할 수 없으므로, 먼저
        # 정상 환율(exchange_rate=1)로 카드를 충분히 충전해 시험 조건을 만든다.
        try:
            ctx.session.authed_request(
                "POST", f"/api/virtual-cards/{card_id}/fund", owner,
                json_body={"amount": "500.00", "exchange_rate": "1"},
            )
        except SessionError:
            return "charge_idempotency 프로브: 카드 충전 실패"
        card = next((c for c in ctx.session.get_own_cards(owner) if str(c.get("id")) == str(card_id)), None)
        if card is None:
            return "charge_idempotency 프로브: 생성한 카드를 조회하지 못함"
        tag = secrets.token_hex(3)
        merchant_token = ctx.session.register_merchant(f"m_{tag}", f"m_{tag}@example.com", "Rt!pass123")
        if merchant_token is None:
            return "charge_idempotency 프로브: 가맹점 등록 실패"
        pairs.append(
            charge.run_charge_idempotency_trial(
                ctx.session, owner, card, merchant_token, order_id=f"ord_{tag}", amount="50.00", repeats=3
            )
        )

    elif input_.probe == "race_transfer":
        accounted = [p for p in ctx.principals if p.account_number]
        if len(accounted) < 2:
            return "race_transfer 프로브: 서로 다른 계좌를 가진 주체가 2명 이상 필요함"
        attacker, victim = accounted[0], accounted[1]
        skeleton = candidates_module.resolve_skeleton(
            "transfer", attacker.id, f"account:{victim.account_number}", ctx.principals, ctx.graph, ctx.target
        )
        if isinstance(skeleton, str):
            return f"race_transfer 프로브: 송금 후보 생성 실패 — {skeleton}"
        candidate = planner_module.build_candidate(
            skeleton,
            {"amount": "10.00", "to_account": victim.account_number, "description": "race-trial"},
            "",
            ctx.baselines,
        )
        if candidate.unit_amount is None or candidate.subject_account is None:
            return "race_transfer 프로브: 단위 금액/대상 계좌 미확정 — INV-1C 판정 재료 부족"
        # 실행은 이미 candidate.method/path/params 로 이뤄진다 — operation 은 이 시점엔
        # 라벨(넛지 추적)용이라 프로브 종류로 바꿔둔다(untried_probes 매핑과 일치).
        candidate = candidate.model_copy(update={"operation": "race_transfer"})
        # overdraft 셋업(잔액을 1건분만 남기고 동시 발사)으로 시험한다 — 실제
        # 송금 경쟁조건(GT F-12)의 플레이버다. INV-1C 오라클은 이 셋업에서 overdraft
        # 를, 채워진 셋업에서는 lost-update 를 각각 잡으므로 상위호환이다.
        trials = burst.run_overdraft_race_candidate(
            ctx.session, ctx.target, candidate, attacker,
            watch_accounts=[str(attacker.account_number), str(victim.account_number)],
            n_concurrent=5, restorer=victim, trials=RACE_TRIALS,
        )
        return _record_race_finding(ctx, candidate, burst.summarize_race(trials))

    elif input_.probe == "loan_limit":
        if owner is None:
            return "loan_limit 프로브: 대출 요청 주체(계좌 보유)가 없음"
        # 음수·0(양수성) + 비정상 대형 금액(최대한도 없음, max_policy 는 프로브가 주장하는 도메인 경계)
        pairs.append(loan.run_loan_amount_policy_probe(ctx.session, owner, amount="-500.00"))
        pairs.append(loan.run_loan_amount_policy_probe(ctx.session, owner, amount="0.00"))
        pairs.append(
            loan.run_loan_amount_policy_probe(
                ctx.session, owner, amount="100000000.00", max_policy="1000000.00"
            )
        )

    elif input_.probe == "sqli_scan":
        if owner is None or not owner.account_number:
            return "sqli_scan: 기준으로 쓸 계좌 있는 주체가 없음"
        acct = owner.account_number
        # boolean-based SQLi 프로브는 GET 경로의 {value} 를 payload 로 치환한다.
        # 기록 path 는 payload 가 아닌 정상 경로로 남겨 GT(G-17/G-02)와 매칭되게 한다.
        targets = [
            ("sqli_check_balance", f"/check_balance/{{value}}", f"/check_balance/{acct}"),
            ("sqli_transactions", f"/transactions/{{value}}", f"/transactions/{acct}"),
        ]
        lines = [
            _record_generic_outcome(
                ctx, op, "GET", record_path, generic_web.probe_sqli_boolean(ctx.session, template, acct)
            )
            for op, template, record_path in targets
        ]
        return "\n".join(lines)

    elif input_.probe == "jwt_forge":
        # 위조 토큰의 payload 구조는 실제 발급 토큰에서 가져온다(부트스트랩된 주체).
        tokened = next((p for p in ctx.principals if p.token), None)
        if tokened is None:
            return "jwt_forge: 참조할 정상 토큰을 가진 주체가 없음"
        import base64 as _b64
        import json as _json
        try:
            seg = tokened.token.split(".")[1]
            seg += "=" * (-len(seg) % 4)
            payload = _json.loads(_b64.urlsafe_b64decode(seg))
        except Exception:
            payload = {"user_id": 1, "is_admin": True}
        payload = {**payload, "is_admin": True}  # 위조 시 권한 승격까지 시도
        outcome = generic_api.probe_jwt_weak_secret(ctx.session, "/api/virtual-cards", "GET", payload)
        return _record_generic_outcome(ctx, "jwt_weak_secret", "GET", "/api/virtual-cards", outcome)

    else:
        return f"알 수 없는 probe: {input_.probe!r}"

    lines = [_record_probe_finding(ctx, candidate, result) for candidate, result in pairs]
    return "\n".join(lines) if lines else f"{input_.probe} 프로브가 결과를 만들지 못함"


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

    if step.action == "run_probe":
        assert isinstance(parsed, RunProbeInput)
        return _handle_run_probe(parsed, ctx)

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


def _finalize(ctx: "AgentContext", reason: str) -> None:
    """`store.finalize()` + 사람이 읽는 트레이스(`transcript.md`) 를 항상 같이 남긴다
    (§ DEBUGGING.md 3.5) — 정상 종료·LLM 의 finish 요청·예외로 인한 중단 전부 여기를 거친다.

    Engagement가 `cleanup.required` 면 CLEANUP phase(engagement §11)도 여기서
    같이 돈다 — 종료 경로가 여럿이라도(정상/LLM finish/예외) 정리를 빼먹지
    않는다. cleanup 자체가 실패해도(예: 대상이 이미 죽음) manifest 확정은
    막지 않는다 — 실패를 조용히 넘기지 않고 기록만 남긴다(§11).
    """
    if ctx.policy.cleanup_required and ctx.created_resources:
        try:
            principal_by_id = {p.id: p for p in ctx.principals}
            records = run_cleanup_phase(ctx.session, ctx.target, ctx.created_resources, principal_by_id)
            for record in records:
                ctx.store.append("cleanup", record.to_dict())
        except Exception:
            logger.exception("CLEANUP phase 자체가 예외로 실패함 — manifest 확정은 계속 진행")

    ctx.store.finalize(reason, usage=ctx.llm_usage)
    write_transcript(ctx.store.db, ctx.store.manifest, ctx.store.run_dir)


def run(
    target: TargetConfig,
    session: Session,
    llm: LLMClient,
    store: RunStore,
    policy: CompiledPolicy,
    *,
    credentials: list[EngagementCredential] | None = None,
    max_rounds: int | None = None,
    on_step: Callable[[Step], None] | None = None,
) -> AgentContext:
    """`on_step`: 스텝이 끝나는 즉시 호출되는 선택적 콜백 — 실시간 콘솔 출력용
    (§ DEBUGGING.md 3.1). `agent/react.py` 는 여기서도 rich/console 을 모른다 —
    콜백 시그니처만 안다.

    `policy`: 승인된 Engagement 를 컴파일한 `CompiledPolicy` — Hard Constraint
    는 `session`(네트워크 경계, 최종 강제)과 여기(사전 검사·계정생성 게이트·
    stop condition)에서 이중으로 강제된다.
    `credentials`: Engagement 가 제공한 테스트 계정(있는 만큼만 로그인, 나머지는
    `policy.allow_account_creation` 에 따라 자동 등록되거나 실패한다 — 계정생성
    판단 자체는 여전히 LLM 이 아니라 `bootstrap_principals` 가 한다, engagement §14).
    """
    budget = Budget(max_rounds=max_rounds) if max_rounds else Budget()
    ctx = AgentContext(target=target, session=session, llm=llm, store=store, budget=budget, policy=policy)

    # Phase 0: 사전 크롤(비인증) — BOOTSTRAP 이 Black-box Auth Discovery 로 로그인/
    # 회원가입 endpoint 를 찾을 수 있게 `/` 에서 1홉만 미리 본다(§ Human
    # pre-analysis 제거 — targets/configs/*.yaml 의 seed_paths 에 의존하지 않는다).
    discovery_pages, protected_path = pre_crawl_for_auth_discovery(session)

    # Phase 1: BOOTSTRAP
    ctx.principals, ctx.legacy_bootstrap_used = bootstrap_principals(
        session,
        target,
        credentials,
        allow_account_creation=policy.allow_account_creation,
        discovery_pages=discovery_pages,
        protected_path=protected_path,
    )
    store.record_legacy_bootstrap(ctx.legacy_bootstrap_used)
    store.record_principals(ctx.principals)  # 목표 채점의 principal→계좌 매핑용 (비밀 제외)
    if ctx.legacy_bootstrap_used:
        logger.warning(
            "Black-box Auth Discovery 실패 — 사람이 작성한 어댑터 설정(LEGACY_BOOTSTRAP)으로 "
            "폴백함: %s (이 실행은 인증 부분에서 완전한 autonomous black-box 가 아님)",
            ctx.legacy_bootstrap_used,
        )
    provisioned = [p for p in ctx.principals if p.extra.get("bootstrap_source") == "provisioned"]
    self_created = [p for p in ctx.principals if p.extra.get("bootstrap_source") != "provisioned"]
    ctx.graph.add_entitlements(entitlement_module.from_self_created(self_created))
    ctx.graph.add_entitlements(entitlement_module.from_provisioned(provisioned))
    # ★ 제공받은(등록하지 않은) 계정은 CLEANUP 대상이 아니다 — 이 실행이 만든
    # 리소스가 아니므로 실수로 정리 시도하지 않는다(engagement §11).
    for p in self_created:
        if p.account_number:
            ctx.created_resources.append(
                CreatedResource(resource_type="account", resource_id=p.account_number, created_by_step=0, principal_id=p.id)
            )

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
        # ★ Operation/Parameter Discovery 우선(§ recon/operation_discovery.py) —
        # 실패하면 정적 baseline_ops(LEGACY_BOOTSTRAP)로 폴백하고 정직하게 기록한다.
        discovered_transfer = discover_and_learn_money_operation(session, target, payer, payee, ctx.baselines)
        if discovered_transfer is not None:
            ctx.discovered_operations["transfer"] = discovered_transfer
        else:
            ctx.legacy_bootstrap_used.append("transfer_operation")
        for op_cfg in target.baseline_ops():
            if op_cfg.get("op") == "transfer" and discovered_transfer is not None:
                continue  # 이미 discovery 로 학습됨 — 정적 config 로 덮어쓰지 않는다
            ctx.baselines.learn(session, target, op_cfg, payer, counterparty=payee)
    store.record_legacy_bootstrap(ctx.legacy_bootstrap_used)

    ctx.templates = candidates_module.load_templates()
    ctx._prev_edge_count = ctx.graph.edge_count()

    system_prompt = prompts.build_react_system(ctx.templates, guidance=policy.guidance)

    # Phase 5~N: ReAct 순환
    # ★ 최상위 try/except — 처리되지 않은 예외(우리 코드의 버그)가 나면 조용히
    # 죽지 않고 로그를 남기고 manifest 를 마무리한 뒤 다시 던진다. 이게 없으면
    # 크래시 시 store.finalize() 가 안 불려 manifest.finished_at 이 계속
    # null 로 남는다(§ DEBUGGING.md 3.4-b).
    try:
        while True:
            stop, reason = termination.should_stop(ctx)
            if stop:
                _finalize(ctx, reason)
                return ctx
            if ctx._finish_requested is not None:
                _finalize(ctx, ctx._finish_requested)
                return ctx

            ctx.round += 1
            edge_count_before = ctx.graph.edge_count()
            hypothesis_counter_before = ctx._hypothesis_counter

            observation_text = _build_observation_text(ctx)
            user_prompt = prompts.build_react_user(observation_text)

            try:
                results = llm.complete_json(system_prompt, user_prompt, ReActStep, temperature=0.3)
                ctx._consecutive_llm_errors = 0
            except LLMError as exc:
                # ★ API 자체의 실패(네트워크·레이트리밋·타임아웃) — 우리 코드의
                # 버그와 구분한다(§ DEBUGGING.md 3.4-a). 이번 스텝만 실패
                # 처리하고 계속 진행한다 — LLMError 가 아닌 예외는 여기서 안
                # 잡는다, 위 최상위 try 로 전파되어 조용히 묻히지 않는다.
                logger.warning("LLM 호출 실패 (step=%d): %s", ctx.round, exc)
                results = []
                ctx._consecutive_llm_errors += 1
                # 레이트리밋 등으로 계속 실패할 때 곧장 다음 스텝을 또 쏘면 같은 제한에
                # 다시 걸린다(실측: 429 6연속, 13초 안에 라운드 소진 — 벤더 SDK 자체
                # 재시도만으로는 부족했다). 실패 횟수에 비례해 잠깐 쉬어 창이 풀릴
                # 시간을 준다 — llm_unavailable() 로 완전히 막히면 그때 종료한다.
                backoff = min(2.0 ** ctx._consecutive_llm_errors, _LLM_BACKOFF_CAP_SECONDS)
                logger.info("LLM 재시도 전 %.0f초 대기 (연속 실패 %d회)", backoff, ctx._consecutive_llm_errors)
                time.sleep(backoff)

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

            # ★ 실측 버그: propose_hypothesis 는 설계상 부작용(그래프 변화)이
            # 없다(§ planning/candidates.py — 가설/실험 분리) — edge_count 만
            # "성장"으로 인정하면, 정찰만 오래 하다 막 가설을 하나 등록한
            # 스텝조차 "무성장"으로 잡혀서 saturated 로 조기 종료된다. 실제
            # 사례: recon 5스텝 + propose_hypothesis 1스텝 = 6스텝 연속 무성장으로
            # 판정되어 execute_experiment 를 시도해보지도 못하고 끝남. 새 가설
            # 등록도 "성장"(=전진)으로 인정한다.
            if ctx.graph.edge_count() > edge_count_before or ctx._hypothesis_counter > hypothesis_counter_before:
                ctx._steps_without_growth = 0
            else:
                ctx._steps_without_growth += 1
            ctx._prev_edge_count = edge_count_before

            store.snapshot_round(ctx.round)
    except Exception as exc:
        logger.exception("ReAct 루프가 처리되지 않은 예외로 중단됨 (step=%d)", ctx.round)
        _finalize(ctx, f"error: {exc.__class__.__name__}")
        raise
