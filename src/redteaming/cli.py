from __future__ import annotations

import argparse
import getpass
import os
from pathlib import Path

from .llm.client import LLMConfig, build_client
from .llm.conversation import Conversation
from .auth import AuthorizationCheck, HttpIdentityProfile
from .browser_session import BrowserSessionError, PlaywrightSessionCapture
from .doctor import run_diagnostics
from .evaluation import EvaluationCase, EvaluationHarness, load_evaluation_cases, render_evaluation
from .runtime import LocalRuntime
from .events import Event
from .policies import load_detector_names, load_policies
from .reporting import render_engagement_json, render_engagement_report
from .orchestration import EngagementOrchestrator, ScenarioPolicyError
from .persistence import SnapshotError, load_snapshot, save_snapshot
from .planning import EngagementPlan
from .ui import ConsoleUI
from .models import Asset


def main() -> int:
    parser = argparse.ArgumentParser(description="Interactive authorized red-team controller")
    parser.add_argument("--no-llm", action="store_true", help="run local command handling without an LLM")
    parser.add_argument("--plain", action="store_true", help="disable ANSI colors and banner styling")
    args = parser.parse_args()
    _load_local_env(Path(os.getenv("REDTEAM_ENV_FILE", ".env")))
    config = None if args.no_llm else LLMConfig.from_env()
    conversation = Conversation(client=build_client(config))
    policy_path = Path(os.getenv("REDTEAM_POLICY_FILE", ".redteaming/policy.json"))
    discovery_policy, recon_policy = load_policies(policy_path)
    detector_names = load_detector_names(policy_path)
    runtime = LocalRuntime(recon_checkpoint=Path(".redteaming/recon-checkpoint.json"),
                           discovery_policy=discovery_policy, recon_policy=recon_policy)
    identities: tuple[HttpIdentityProfile, ...] = ()
    state_path = Path(".redteaming/engagement.json")
    current_plan = None
    try:
        restored_events, current_plan = load_snapshot(
            state_path, runtime.assets, runtime.evidence, runtime.findings, runtime.exploration
        )
        runtime.events.history.extend(restored_events)
        runtime.publisher.seed_existing(runtime.findings)
    except SnapshotError as exc:
        print(f"engagement snapshot ignored: {exc}")
    ui = ConsoleUI(plain=args.plain)
    _bind_runtime_events(runtime, ui)
    ui.banner(model=_model_name(conversation), scope=_scope_name(conversation))
    while True:
        try:
            text = ui.prompt().strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not text:
            continue
        if text in {"/quit", "/exit"} or _is_exit_request(text):
            return 0
        if text == "/help" or _is_help_request(text):
            print("""자연어로 요청할 수 있습니다.

승인된 https://target.example을 테스트 대상으로 등록해줘
다른 승인 서버 https://other.example도 추가해줘
초기 정찰과 취약점 점검을 실행해줘
발견된 자산과 열린 포트를 보여줘
로그인 세션을 권한 비교용으로 등록해줘
다른 계정의 접근 권한을 비교 검증해줘
LiteLLM으로 연결해줘
현재 상태를 보여줘
증거 기반 리포트를 생성해줘
대화 컨텍스트를 초기화해줘

Ctrl+C는 실행 중인 평가를 일시중지하고, 종료라고 입력하면 종료합니다.""")
            continue
        if text == "/doctor" or _is_doctor_request(text):
            for item in run_diagnostics():
                ui.info(f"doctor: {item.name}={item.status} ({item.detail})")
            continue
        if _is_status_request(text):
            ui.assistant(conversation.reply("/status"))
            continue
        if _is_llm_status_request(text):
            ui.assistant(conversation.connection_status())
            continue
        if _is_clear_request(text):
            ui.assistant(conversation.reply("/clear"))
            continue
        if text == "/scope-add":
            ui.assistant("Usage: /scope-add host.example or /scope-add host1 host2")
            continue
        if text.startswith("/identity-browser") or _is_browser_identity_request(text):
            if conversation.scope is None:
                ui.assistant("먼저 승인된 Scope를 등록하세요.")
                continue
            url = text[len("/identity-browser"):].strip() if text.startswith("/identity-browser") else ""
            url = url or input("authorized login URL: ").strip()
            name = input("profile name: ").strip()
            try:
                profile = PlaywrightSessionCapture().capture(url, conversation.scope, name)
            except BrowserSessionError as exc:
                ui.assistant(str(exc))
                continue
            identities = tuple(item for item in identities if item.name != profile.name) + (profile,)
            ui.info(f"identity profile registered for this session: {profile.name}")
            continue
        if text == "/identity" or _is_identity_request(text):
            profile = _prompt_identity_profile()
            if profile is not None:
                identities = tuple(item for item in identities if item.name != profile.name) + (profile,)
                ui.info(f"identity profile registered for this session: {profile.name}")
            continue
        if text == "/identities":
            ui.info("identities=" + (", ".join(item.name for item in identities) if identities else "anonymous"))
            continue
        if text == "/authz-check" or _is_authorization_request(text):
            if conversation.scope is None:
                ui.assistant("먼저 승인된 Scope를 등록하세요.")
                continue
            if len(identities) < 2:
                ui.assistant("권한 비교에는 서로 다른 세션 Identity가 2개 이상 필요합니다. 먼저 /identity 또는 /identity-browser를 사용하세요.")
                continue
            object_url = input("operator-declared object URL (GET only): ").strip()
            if not object_url or not conversation.scope.allows(object_url):
                ui.assistant("객체 URL이 현재 승인 Scope 밖이거나 비어 있습니다.")
                continue
            names = [item.name for item in identities]
            print("available identities: " + ", ".join(names))
            owner = input("owner identity: ").strip()
            comparisons = tuple(item.strip() for item in input("comparison identities (comma-separated): ").split(",") if item.strip())
            if owner not in names or not comparisons or any(item not in names or item == owner for item in comparisons):
                ui.assistant("owner와 comparison에는 등록된 서로 다른 Identity를 입력하세요.")
                continue
            label = input("object label (optional): ").strip() or "operator-declared object"
            from urllib.parse import urlparse
            parsed_object = urlparse(object_url)
            object_key = parsed_object.path or "/"
            if parsed_object.query:
                object_key += "?" + parsed_object.query
            asset = runtime.assets.upsert(Asset(kind="endpoint", canonical_value=object_url,
                                                properties={"discovered_by": "operator-authorization-check"}))
            try:
                ui.status(model=_model_name(conversation), scope=_scope_name(conversation), phase="verify")
                finding = runtime.run_authorization_check(
                    asset,
                    AuthorizationCheck(object_key=object_key,
                                       owner_identity=owner, comparison_identities=comparisons, label=label),
                    conversation.scope, identities,
                )
                save_snapshot(state_path, runtime.assets, runtime.evidence, runtime.findings,
                              runtime.exploration, runtime.events.history, current_plan)
                ui.assistant("인가 비교 Finding이 생성되었습니다." if finding else
                             "인가 우회는 재현되지 않았거나 inconclusive입니다. 증거는 세션에 보존되었습니다.")
            except (KeyError, RuntimeError, ValueError) as exc:
                ui.assistant(f"인가 비교 실행 실패: {exc}")
            finally:
                ui.status(model=_model_name(conversation), scope=_scope_name(conversation), phase="idle")
            continue
        if text.startswith("/evaluate") or _is_evaluation_request(text):
            raw_categories = text[len("/evaluate"):].strip() if text.startswith("/evaluate") else ""
            if raw_categories.lower().endswith(".json") and Path(raw_categories).is_file():
                try:
                    cases = load_evaluation_cases(Path(raw_categories))
                    harness = EvaluationHarness()
                    for case in cases:
                        print(render_evaluation(harness.evaluate(case, runtime.findings)))
                except (OSError, ValueError) as exc:
                    ui.assistant(f"evaluation file could not be loaded: {exc}")
                continue
            if not raw_categories:
                raw_categories = input("expected finding categories (comma-separated): ").strip()
            categories = frozenset(item.strip() for item in raw_categories.split(",") if item.strip())
            if not categories:
                ui.assistant("At least one ground-truth category is required.")
                continue
            result = EvaluationHarness().evaluate(
                EvaluationCase("interactive", categories), runtime.findings
            )
            print(render_evaluation(result))
            continue
        if text in {"/onboard", "/connect", "/llm setup"}:
            _connect_menu(conversation)
            ui.status(model=_model_name(conversation), scope=_scope_name(conversation), phase="idle")
            continue
        report_intent = (
            text.startswith("/report")
            or text.startswith("/generate-report")
            or _is_report_request(text)
            or _is_explicit_report_request(text)
        )
        assessment_intent = (
            not _is_scope_registration_request(text)
            and (text == "/recon"
            or _is_recon_request(text)
            or _is_explicit_recon_request(text))
        )
        # A combined request executes the assessment first; report-only
        # language must never accidentally trigger a new scan.
        if assessment_intent:
            if conversation.scope is None:
                scope_response = conversation.reply(text)
                if conversation.scope is None:
                    ui.assistant("먼저 승인된 대상이 필요합니다. 예: '<host:port>를 승인된 테스트 대상으로 등록해줘'")
                    continue
                ui.info(scope_response)
            ui.status(model=_model_name(conversation), scope=_scope_name(conversation), phase="recon")
            try:
                if _scope_requires_new_engagement(current_plan, conversation.scope):
                    runtime = LocalRuntime(recon_checkpoint=Path(".redteaming/recon-checkpoint.json"),
                                           discovery_policy=discovery_policy,
                                           recon_policy=recon_policy)
                    _bind_runtime_events(runtime, ui)
                    current_plan = None
                    identities = ()
                    ui.info("new authorized Scope detected; starting a separate engagement")
                evidence_before = len(runtime.evidence.items)
                orchestrator = EngagementOrchestrator(runtime, conversation.client)
                resume_plan = _resume_plan_for_request(text, current_plan, conversation.scope)
                if current_plan is not None and resume_plan is None and _is_replan_request(text):
                    runtime.events.publish(Event("planner.replan_triggered", {
                        "previous_plan": current_plan.id,
                        "reason": "explicit operator replan request",
                    }))
                result = orchestrator.execute(
                    text, conversation.scope, identities=identities, resume_plan=resume_plan,
                    detector_names=detector_names,
                )
                plan = result.plan
                current_plan = plan
                ui.info(f"[plan] {plan.source} scenario={plan.scenario_id} objective={plan.objective}")
                for step in plan.steps:
                    ui.info(f"[plan:{step.action}] {step.objective}")
                runs = list(result.runs)
                discovered = sum(len(run.assets) for run in runs)
                findings = list(result.findings)
                evidence = len(runtime.evidence.items) - evidence_before
                save_snapshot(state_path, runtime.assets, runtime.evidence, runtime.findings,
                              runtime.exploration, runtime.events.history, current_plan)
                ui.assistant(
                     f"정찰 완료: {discovered}개 Asset 발견, {evidence}개 raw evidence 저장, "
                     f"검증된 Finding {len(findings)}개 생성 "
                     f"(현재 누적 Finding {len(runtime.findings)}개)"
                )
                if report_intent:
                    report = render_engagement_report(
                        list(runtime.assets.assets.values()),
                        list(runtime.evidence.items.values()),
                        runtime.findings, runtime.exploration,
                        runtime.events.history, current_plan,
                    )
                    print(report)
            except KeyboardInterrupt:
                save_snapshot(state_path, runtime.assets, runtime.evidence, runtime.findings,
                              runtime.exploration, runtime.events.history, current_plan)
                ui.assistant("assessment paused; completed checkpoints are retained for the next /recon")
            except ScenarioPolicyError as exc:
                save_snapshot(state_path, runtime.assets, runtime.evidence, runtime.findings,
                              runtime.exploration, runtime.events.history, current_plan)
                ui.assistant(str(exc))
            except (KeyError, RuntimeError, ValueError) as exc:
                save_snapshot(state_path, runtime.assets, runtime.evidence, runtime.findings,
                              runtime.exploration, runtime.events.history, current_plan)
                ui.assistant(f"정찰 실행 실패: {exc}")
            ui.status(model=_model_name(conversation), scope=_scope_name(conversation), phase="idle")
            continue
        if report_intent:
            json_requested = _is_json_report_request(text)
            report = (render_engagement_json if json_requested else render_engagement_report)(
                list(runtime.assets.assets.values()), list(runtime.evidence.items.values()),
                runtime.findings, runtime.exploration, runtime.events.history, current_plan)
            if text.startswith("/generate-report"):
                output_path = text[len("/generate-report"):].strip()
            elif text.startswith("/report"):
                output_path = text[len("/report"):].strip()
            elif json_requested:
                output_path = "reports/engagement.json"
            else:
                output_path = ""
            if output_path:
                destination = Path(output_path).expanduser()
                allowed_suffixes = {".json"} if json_requested else {".md", ".markdown"}
                if destination.suffix.lower() not in allowed_suffixes:
                    destination = destination.with_suffix(".json" if json_requested else ".md")
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(report, encoding="utf-8")
                ui.info(f"report written: {destination}")
            else:
                print(report)
            save_snapshot(state_path, runtime.assets, runtime.evidence, runtime.findings,
                          runtime.exploration, runtime.events.history, current_plan)
            continue
        if text == "/assets" or _is_assets_request(text):
            if not runtime.assets.assets:
                ui.assistant("아직 발견된 Asset이 없습니다. /recon을 실행하세요.")
            else:
                for asset in runtime.assets.assets.values():
                    print(f"- {asset.kind}: {asset.canonical_value}")
            continue
        ui.status(model=_model_name(conversation), scope=_scope_name(conversation), phase="planning")
        answer = conversation.reply(text)
        ui.status(model=_model_name(conversation), scope=_scope_name(conversation), phase="idle")
        ui.assistant(answer)


def _model_name(conversation: Conversation) -> str:
    config = getattr(conversation.client, "config", None)
    return str(getattr(config, "model", None) or "not connected")


def _load_local_env(path: Path) -> None:
    """Load non-overriding, repository-local LLM settings without dotenv."""
    if not path.is_file():
        return
    allowed = {
        "REDTEAM_LLM_BACKEND", "REDTEAM_LLM_MODEL", "REDTEAM_OAUTH_MODEL",
        "REDTEAM_LLM_BASE_URL", "REDTEAM_LLM_REASONING_EFFORT",
        "REDTEAM_LLM_SEND_OBSERVATION_METADATA", "REDTEAM_LLM_API_KEY",
        "OPENAI_API_KEY", "LITELLM_MODEL", "LITELLM_API_KEY",
    }
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key not in allowed:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


def _bind_runtime_events(runtime: LocalRuntime, ui: ConsoleUI) -> None:
    labels = {
        "recon.started": "recon: approved target scan started",
        "recon.completed": "recon: assets normalized into Asset Graph",
        "recon.resumed": "recon: checkpoint resume found no remaining approved ports",
        "http.identification.started": "http: identifying reachable services",
        "http.started": "http: probing authorized HTTP path",
        "http.completed": "http: response captured as evidence",
        "http.failed": "http: service did not respond as HTTP",
        "http.identification.completed": "http: service identification completed",
        "http.surface.discovered": "http: dynamic surface discovered",
        "http.api.parameter.discovered": "http: API query parameter discovered",
        "http.asset.anchored": "graph: HTTP endpoint anchored to service",
        "http.rate_limited": "policy: HTTP request rate limit reached",
        "detector.started": "detector: running registered detectors",
        "detector.candidate": "detector: candidate created",
        "verification.started": "verify: replaying candidate request",
        "verification.completed": "verify: replay completed",
        "finding.created": "finding: evidence-backed Finding created",
        "agent.role.started": "agent: role started",
        "agent.role.completed": "agent: role completed",
        "planner.replan_triggered": "planner: explicit replan requested",
        "report.ready": "report: ready for explicit generation",
        "hypothesis.agent.started": "hypothesis: specialist started",
        "hypothesis.agent.completed": "hypothesis: specialist completed",
        "hypothesis.created": "hypothesis: candidate hypothesis created",
        "hypothesis.gated": "hypothesis: policy gate evaluated",
        "hypothesis.prioritized": "hypothesis: verification order selected",
        "finding.duplicate_skipped": "finding: existing validated Finding retained",
        "finding.grouped": "finding: endpoints grouped under one root cause",
        "agent.policy.blocked": "policy: scenario execution blocked",
        "objective.graph.validated": "objective: graph validated",
        "objective.started": "objective: started",
        "objective.completed": "objective: completed",
        "objective.resumed": "objective: resumed from checkpoint",
    }

    def show(event) -> None:
        label = labels.get(event.name, event.name)
        details = " ".join(f"{key}={value}" for key, value in event.payload.items()
                           if key in {"target", "url", "status", "asset_count", "observation_count",
                                      "candidate", "title", "identity", "paths", "role", "action", "source",
                                      "agent", "category", "id", "accepted", "reason", "parameter", "path"})
        ui.info(f"[{label}]" + (f" {details}" if details else ""))

    for name in labels:
        runtime.events.subscribe(name, show)


def _scope_name(conversation: Conversation) -> str:
    scope = conversation.scope
    return ", ".join(scope.allowed_hosts) if scope else "not set"


def _scope_requires_new_engagement(plan: EngagementPlan | None, scope) -> bool:
    """Separate replacement Scopes while retaining additive /scope-add runs."""
    if plan is None or scope is None:
        return False
    previous = set(plan.scope_hosts)
    current = set(scope.allowed_hosts)
    return not previous.issubset(current)


def _resume_plan_for_request(text: str, plan: EngagementPlan | None,
                             scope) -> EngagementPlan | None:
    """Reuse a snapshot plan only for an unqualified continuation request.

    A new scenario request must still go through planning. This keeps resume
    deterministic without making an old plan silently control a materially
    different operator intent.
    """
    if plan is None or scope is None or tuple(plan.scope_hosts) != tuple(scope.allowed_hosts):
        return None
    normalized = text.lower()
    if _is_replan_request(text):
        return None
    return plan


def _is_replan_request(text: str) -> bool:
    normalized = text.lower().replace(" ", "")
    return any(token in normalized for token in (
        "replan", "새계획", "새로운계획", "재계획", "계획을다시", "다른시나리오",
        "freshplan", "freshassessment", "새로운시나리오",
    ))


def _is_scope_registration_request(text: str) -> bool:
    """Recognize scope-only language so registration cannot start execution."""
    normalized = text.lower().replace(" ", "")
    registration = ("등록", "추가", "허용", "승인", "scope")
    analysis = ("정찰", "스캔", "recon", "scan", "포트", "서비스", "취약점",
                "분석", "점검", "검사", "실행", "진단")
    target = ("http://", "https://", "localhost", "127.", "대상", "서버", "scope")
    return any(word in normalized for word in registration) and any(
        word in normalized for word in target
    ) and not any(word in normalized for word in analysis)


def _is_recon_request(text: str) -> bool:
    normalized = text.lower().replace(" ", "")
    if any(phrase in normalized for phrase in (
        "정찰실행", "정찰시작", "초기정찰", "포트정찰", "서버분석", "취약점점검",
        "취약점진단", "스캔해줘", "분석해줘",
    )):
        return True
    # Keep the deterministic control-plane route usable without requiring an
    # exact command phrase.  This is intent-shape matching only: the actual
    # target, ports, and checks still come from Scope and policy.
    discovery_terms = ("정찰", "포트", "서비스", "서버", "대상", "취약점", "스캔", "recon", "scan")
    action_terms = ("실행", "시작", "확인", "찾", "분석", "점검", "검사", "알려", "진행", "run", "check")
    if any(term in normalized for term in discovery_terms) and any(term in normalized for term in action_terms):
        return True
    return any(phrase in normalized for phrase in (
        "정찰실행", "정찰시작", "정찰해줘", "포트스캔실행", "포트정찰실행",
        "포트찾아줘", "열린포트", "서버정찰", "초기정찰", "먼저정찰",
        "초기스캔", "취약점찾아줘", "취약점찾아",
    ))


def _is_report_request(text: str) -> bool:
    normalized = text.lower().replace(" ", "")
    if any(phrase in normalized for phrase in ("보고서보여줘", "리포트보여줘", "분석결과보여줘")):
        return True
    if any(phrase in normalized for phrase in ("보고서만들어줘", "정찰결과보고서", "증거보고서", "리포트작성")):
        return True
    report_noun = any(document in normalized for document in ("리포트", "보고서"))
    report_noun = report_noun or ("분석결과" in normalized and "문서" in normalized)
    return report_noun and any(
        action in normalized for action in ("보여", "요약", "정리", "작성", "만들", "생성")
    )


def _is_explicit_report_request(text: str) -> bool:
    normalized = text.lower().replace(" ", "")
    if any(phrase in normalized for phrase in ("리포트생성", "보고서생성", "리포트만들어", "보고서만들어")):
        return True
    return (
        ("generate" in normalized and "report" in normalized)
        or ("create" in normalized and "report" in normalized)
        or "generatereport" in normalized
        or "리포트생성" in normalized
        or "보고서생성" in normalized
    )


def _is_json_report_request(text: str) -> bool:
    normalized = text.lower().replace(" ", "")
    return ".json" in normalized or "json리포트" in normalized or "json보고서" in normalized


def _is_explicit_recon_request(text: str) -> bool:
    normalized = text.lower().replace(" ", "")
    return (
        "recon" in normalized
        or "scan" in normalized
        or ("analyze" in normalized and ("server" in normalized or "target" in normalized))
        or "정찰실행" in normalized
        or "분석해줘" in normalized
        or "취약점찾아" in normalized
    )


def _is_assets_request(text: str) -> bool:
    normalized = text.lower().replace(" ", "")
    if any(phrase in normalized for phrase in ("자산목록", "발견된자산", "자산보여줘")):
        return True
    return any(phrase in normalized for phrase in (
        "발견된자산보여줘", "열린포트보여줘", "자산목록", "포트목록",
        "showassets", "listassets", "showopenports", "listopenports",
        "showports", "listports",
    )) or (
        any(term in normalized for term in ("자산", "포트", "서비스"))
        and any(action in normalized for action in ("목록", "나열", "보여", "알려", "확인"))
    )


def _is_identity_request(text: str) -> bool:
    normalized = text.lower().replace(" ", "")
    if any(phrase in normalized for phrase in ("인증프로필", "인증정보등록", "로그인세션", "토큰등록")):
        return True
    return any(phrase in normalized for phrase in (
        "addidentity", "identityprofile", "authprofile", "addcredential",
        "token등록", "쿠키등록", "인증프로필",
    ))


def _is_browser_identity_request(text: str) -> bool:
    normalized = text.lower().replace(" ", "")
    return ("브라우저" in normalized and
            any(token in normalized for token in ("로그인", "세션", "인증")))


def _is_authorization_request(text: str) -> bool:
    normalized = text.lower().replace(" ", "")
    if any(phrase in normalized for phrase in (
        "권한비교", "인가비교", "접근권한검증", "idor검증", "bola검증",
        "authorizationcheck", "accesscontrolcheck", "checkauthorization",
    )):
        return True
    return (
        any(term in normalized for term in ("사용자", "계정", "아이디", "identity"))
        and any(term in normalized for term in ("권한", "접근", "볼수", "비교", "인가"))
        and any(term in normalized for term in ("확인", "검증", "테스트", "점검", "해줘"))
    )


def _is_evaluation_request(text: str) -> bool:
    normalized = text.lower().replace(" ", "")
    if "평가" in normalized:
        return True
    return "evaluation" in normalized or "평가" in normalized


def _is_doctor_request(text: str) -> bool:
    normalized = text.lower().replace(" ", "")
    return any(token in normalized for token in ("환경점검", "설치확인", "의존성확인", "diagnose", "doctor"))


def _is_help_request(text: str) -> bool:
    normalized = text.lower().replace(" ", "")
    return normalized in {"도움말", "사용법", "help", "무엇을할수있어", "어떻게사용해"}


def _is_status_request(text: str) -> bool:
    normalized = text.lower().replace(" ", "")
    return any(token in normalized for token in ("현재상태", "상태보여줘", "승인범위보여줘", "scope보여줘"))


def _is_llm_status_request(text: str) -> bool:
    normalized = text.lower().replace(" ", "")
    return any(token in normalized for token in (
        "llm연결상태", "litellm연결상태", "인증상태", "모델연결상태",
        "llm상태", "litellm상태", "checkllm", "checklitellm",
    ))


def _is_clear_request(text: str) -> bool:
    normalized = text.lower().replace(" ", "")
    return normalized in {"대화초기화", "컨텍스트초기화", "clear", "reset"}


def _is_exit_request(text: str) -> bool:
    normalized = text.lower().replace(" ", "")
    return normalized in {"종료", "끝내", "끝내줘", "quit", "exit"}


def _prompt_identity_profile() -> HttpIdentityProfile | None:
    print("Temporary auth profile (stored only in this CLI session).")
    name = input("profile name (e.g. owner/viewer): ").strip()
    if not name:
        return None
    headers: list[tuple[str, str]] = []
    while True:
        header_name = input("header name (Authorization/Cookie/custom, blank when done): ").strip()
        if not header_name:
            break
        header_value = getpass.getpass("header value (hidden, never persisted): ")
        if header_value:
            headers.append((header_name, header_value))
    return HttpIdentityProfile(name, tuple(headers))


def _connect_menu(conversation) -> None:
    print("\nLLM 연결 설정")
    print("1) 기존 OAuth/키 없는 provider 설정 사용")
    print("2) API key 직접 입력")
    print("3) 현재 설정 유지")
    mode = input("선택 [1/2/3] (기본 1): ").strip() or "1"
    if mode == "1":
        print(conversation.use_existing_oauth())
        return
    if mode == "3":
        print("현재 설정을 유지합니다.")
        return
    print("API key는 파일에 저장하지 않고 현재 CLI 세션에만 사용합니다.")
    backend = input("backend [litellm/direct-litellm/openai-compatible] (기본 litellm): ").strip() or "litellm"
    model = input("model (예: chatgpt/gpt-5.5, openai/gpt-5.5): ").strip()
    if not model:
        print("모델은 비워둘 수 없습니다.")
        return
    api_key = getpass.getpass("API key (없으면 Enter, 키 없는 로컬 설정 사용): ")
    os.environ["REDTEAM_LLM_BACKEND"] = backend
    os.environ["REDTEAM_LLM_MODEL"] = model
    if api_key:
        os.environ["REDTEAM_LLM_API_KEY"] = api_key
    else:
        os.environ.pop("REDTEAM_LLM_API_KEY", None)
    config = LLMConfig.from_env()
    print(conversation.configure(config) if config else "LLM 설정을 만들지 못했습니다.")


if __name__ == "__main__":
    raise SystemExit(main())
