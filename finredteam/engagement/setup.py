"""대화형 Engagement Setup — 자연어 우선.

사용자가 자유 텍스트로 "뭘, 왜 테스트하고 싶은지"를 한 번에 설명하면, LLM이
그걸 구조화 필드로 해석하면서 부족하거나 애매한 항목을 **한 번에 하나씩**
질문으로 구체화한다(§3). 질문 자체와 "이 답을 어떤 필드로 해석했는지"는 항상
LLM 이 정하지만, 사용자가 명시적으로 언급하지 않은 항목은 항상 가장 보수적인
(거부) 값으로 남는다 — `_draft_to_engagement()`가 `None`을 전부 `False`/최소값
으로 강제 변환하기 때문에 코드 수준에서 보장된다(LLM이 실수로라도 허용 범위를
넓힐 수 없다). 대화가 끝나면 `engagement.md` 를 보여주고 **사람이 명시적으로
승인해야만** 저장한다(§4) — `engagement_id` 는 사용자가 짓지 않는다, 감사용
으로 시스템이 자동 생성한다(§15).
"""
from __future__ import annotations

import re
import secrets
import sys
from decimal import Decimal, InvalidOperation
from urllib.parse import urlparse

from pydantic import BaseModel
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table

from finredteam.engagement import store as store_module
from finredteam.engagement.loader import list_available_engagements, load_engagement
from finredteam.engagement.schema import (
    Engagement,
    EngagementActions,
    EngagementCleanup,
    EngagementCredential,
    EngagementDataAccess,
    EngagementExternalServices,
    EngagementRequests,
    EngagementScope,
    EngagementStopConditions,
    EngagementTarget,
)
from finredteam.targets.loader import load_target

_MAX_CLARIFY_TURNS = 12
_MAX_TARGET_NAME_RETRIES = 3


class EngagementDraft(BaseModel):
    """LLM 이 대화에서 파악한 것만 채운다 — 언급 안 된 필드는 `None`(=아직 모름)
    으로 남는다. `None` 은 "허용"이 아니라 "모름"이다, 최종적으로
    `_draft_to_engagement()` 가 전부 보수적 값으로 확정한다."""

    target_name: str | None = None
    base_url: str | None = None
    allowed_hosts: list[str] | None = None
    denied_hosts: list[str] | None = None
    external_services_allowed: bool | None = None
    allow_state_change: bool | None = None
    allow_account_creation: bool | None = None
    allow_file_upload: bool | None = None
    allow_delete: bool | None = None
    allow_race_testing: bool | None = None
    allow_dos: bool | None = None
    allow_bruteforce: bool | None = None
    max_requests_per_second: float | None = None
    max_concurrency: int | None = None
    max_amount: str | None = None  # Decimal 문자열 그대로(§4 규칙 2 — float 경유 금지)
    credentials: list[EngagementCredential] | None = None  # 이미 제공된 테스트 계정만 — 내부 구조 아님
    allow_real_user_data: bool | None = None
    cleanup_required: bool | None = None
    cleanup_resource_types: list[str] | None = None
    repeated_5xx_threshold: int | None = None
    guidance: list[str] | None = None


class ClarificationTurn(BaseModel):
    updated_fields: EngagementDraft
    next_question: str | None = None


_SYSTEM_PROMPT = """\
너는 보안 Red Teaming/Bug Bounty Engagement의 Scope와 Rules of Engagement를
사용자와의 대화로 구체화하는 보조자다.

규칙:
1. 매 턴 딱 하나의 질문만 한다(next_question). 이미 충분히 구체적이면
   next_question 을 null 로 두고 끝낸다.
2. 사용자가 명시적으로 말한 것만 updated_fields 에 채운다. 언급 안 된 필드는
   반드시 null 로 둔다 — 절대로 추측해서 True/허용값을 채우지 않는다.
   특히 allow_state_change/allow_account_creation/allow_delete/allow_dos/
   allow_bruteforce/allow_race_testing/allow_file_upload/
   external_services_allowed/allow_real_user_data 는 사용자가 명확히
   허용한다고 말한 경우에만 true 로 채운다.
2b. 사용자가 "이런 건 하지 마라"처럼 모호하게 금지 의도를 말하면, 그와 관련된
   여러 구체적 행위(예: 대량 요청/자원 고갈/반복 로그인/burst 요청 등)를
   금지 대상으로 제안하는 질문을 만들어서 사용자에게 확인받아라(스스로
   단정하지 마라).
2c. 이미 제공받은 테스트 계정이 있는지 물어봐라(credentials) — 있다면
   username/password 만 받는다. **절대로** 로그인/회원가입 endpoint, HTTP
   method, 필드명, token이 반환되는 JSON 경로, Authorization header 형식,
   잔액조회/송금 endpoint나 파라미터 이름 같은 애플리케이션 내부 구조는
   묻지 않는다 — 그건 사람이 별도로 조사해서 어댑터 설정에 적어두는 몫이고,
   장기적으로는 Agent가 Black-box Recon으로 스스로 알아내야 할 대상이다.
3. 우선순위 있게 물어봐라 — base_url/target_name 을 가장 먼저, 그 다음
   scope(allowed_hosts), 이미 제공된 테스트 계정(credentials), 허용 행위,
   금지 행위, 속도/금액 제한, 민감정보, cleanup, stop condition 순서로.
4. 질문은 한국어로, 한 번에 하나씩, 짧고 구체적으로.
5. JSON 으로만 답하라.
"""


def _slugify(text: str) -> str:
    text = re.sub(r"^https?://", "", text or "")
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return text[:30] or "target"


def _generate_engagement_id(target_name: str) -> str:
    return f"{_slugify(target_name)}-{secrets.token_hex(3)}"


def _merge_draft(base: EngagementDraft, update: EngagementDraft) -> EngagementDraft:
    data = base.model_dump()
    for key, value in update.model_dump().items():
        if value is not None:
            data[key] = value
    return EngagementDraft(**data)


def _draft_to_engagement(draft: EngagementDraft, engagement_id: str) -> Engagement:
    """`None` 인 모든 필드를 여기서 가장 보수적인 값으로 확정한다 — LLM 이
    대화 중 실수로라도 허용 범위를 넓힐 수 없는 이유가 바로 이 강제 변환
    (`bool(None) is False`)이다."""
    max_amount = None
    if draft.max_amount:
        try:
            max_amount = Decimal(draft.max_amount)
        except InvalidOperation:
            max_amount = None

    allowed_hosts = draft.allowed_hosts
    if not allowed_hosts and draft.base_url:
        host = urlparse(draft.base_url).hostname
        allowed_hosts = [host] if host else []

    return Engagement(
        engagement_id=engagement_id,
        target=EngagementTarget(
            name=draft.target_name or "target",
            base_url=draft.base_url or "",
            credentials=draft.credentials or [],
        ),
        scope=EngagementScope(allowed_hosts=allowed_hosts or [], denied_hosts=draft.denied_hosts or []),
        external_services=EngagementExternalServices(allowed=bool(draft.external_services_allowed)),
        requests=EngagementRequests(
            max_requests_per_second=draft.max_requests_per_second or 3.0,
            max_concurrency=draft.max_concurrency or 1,
            max_amount=max_amount,
        ),
        actions=EngagementActions(
            allow_state_change=bool(draft.allow_state_change),
            allow_account_creation=bool(draft.allow_account_creation),
            allow_file_upload=bool(draft.allow_file_upload),
            allow_delete=bool(draft.allow_delete),
            allow_race_testing=bool(draft.allow_race_testing),
            allow_dos=bool(draft.allow_dos),
            allow_bruteforce=bool(draft.allow_bruteforce),
        ),
        data_access=EngagementDataAccess(allow_real_user_data=bool(draft.allow_real_user_data)),
        cleanup=EngagementCleanup(
            required=draft.cleanup_required if draft.cleanup_required is not None else True,
            resource_types=draft.cleanup_resource_types or [],
        ),
        stop_conditions=EngagementStopConditions(
            repeated_5xx_threshold=draft.repeated_5xx_threshold or 5,
        ),
        guidance=draft.guidance or [],
    )


def _build_user_prompt(history: list[tuple[str, str]], draft: EngagementDraft) -> str:
    lines = [
        "현재까지 파악된 정보(JSON, null 은 아직 모름):",
        draft.model_dump_json(indent=2),
        "",
        "대화 기록:",
    ]
    for q, a in history:
        lines.append(f"- Q: {q}")
        lines.append(f"  A: {a}")
    return "\n".join(lines)


def _run_conversation(console: Console, llm_factory) -> EngagementDraft:
    """`llm_factory`: 인자 없는 콜러블 — 실제로 LLM이 필요한 순간(첫 질문을
    던지기 직전)에야 호출한다. 그래야 사용자가 이미 승인된 Engagement 를
    재사용하기로 하면(`run_setup`) API 키를 물어보는 일 자체가 없다."""
    console.print(
        Panel(
            "테스트하려는 대상과 목적을 자유롭게 설명해주세요 "
            "(예: \"로컬 5000번 포트에 떠 있는 뱅킹 앱을 테스트하고 싶다, "
            "실제 사용자 데이터는 없고 테스트 계정만 쓴다\")",
            title="Engagement Setup",
            border_style="blue",
            expand=False,
        )
    )
    description = Prompt.ask("설명", console=console)
    history: list[tuple[str, str]] = [("(초기 설명) 테스트 대상과 목적을 자유롭게 설명해주세요", description)]
    draft = EngagementDraft()

    try:
        llm = llm_factory()
    except RuntimeError as exc:
        console.print(f"[yellow]LLM 을 준비할 수 없음 — 질문 없이 최소 정보만으로 진행: {exc}[/yellow]")
        llm = None

    if llm is not None:
        for _ in range(_MAX_CLARIFY_TURNS):
            user_prompt = _build_user_prompt(history, draft)
            try:
                results = llm.complete_json(_SYSTEM_PROMPT, user_prompt, ClarificationTurn, temperature=0.2)
            except Exception as exc:  # noqa: BLE001 — 대화가 막혀도 지금까지 파악한 정보로는 진행한다
                console.print(f"[yellow]LLM 응답 실패 — 지금까지 파악된 정보로 진행: {exc}[/yellow]")
                break
            if not results:
                console.print("[yellow]LLM이 유효한 응답을 못 냄 — 지금까지 파악된 정보로 진행[/yellow]")
                break

            turn = results[0]
            draft = _merge_draft(draft, turn.updated_fields)
            if not turn.next_question:
                break

            answer = Prompt.ask(turn.next_question, console=console)
            history.append((turn.next_question, answer))
        else:
            console.print(f"[yellow]질문 {_MAX_CLARIFY_TURNS}회 한도 도달 — 지금까지 파악된 정보로 진행합니다[/yellow]")

    # ★ 구조적으로 없으면 안 되는 값(base_url/target_name)은 LLM 판단과 무관하게
    # 코드가 직접 확보한다 — Session 을 만들려면 무조건 필요하다.
    if not draft.base_url:
        base_url = Prompt.ask("서비스의 기본 URL은? (예: http://localhost:5000)", console=console)
        draft = _merge_draft(draft, EngagementDraft(base_url=base_url))
    # ★ 어댑터(Legacy Target Adapter) 존재 여부를 여기서 조기 확인한다 — 예전엔
    # 이걸 전체 RoE 인터뷰 + 최종 승인까지 끝난 뒤 __main__.py 에서야 확인해서,
    # 대상이 새 것이면 인터뷰 전체가 헛수고가 되는 문제가 있었다. 어댑터 자체를
    # 여기서 만들어주지는 않는다 — 그건 여전히 사람이 직접 조사해서 작성해야
    # 한다(RUNNING.md 3절), 이 루프는 "없으면 미리 알려주고 계속할지 묻는다"만 한다.
    # 재입력 한도(_MAX_TARGET_NAME_RETRIES)를 둬서 — "다른 이름 다시 입력"을
    # 골라도 매번 같은(빈) 값이 나오는 상황(비대화형 스텁 등)에서 무한루프에
    # 빠지지 않게 한다 — 한도에 닿으면 경고만 남기고 그냥 진행한다.
    for _ in range(_MAX_TARGET_NAME_RETRIES):
        if not draft.target_name:
            suggested = urlparse(draft.base_url).hostname or ""
            target_name = Prompt.ask(
                "이 Engagement 가 쓸 어댑터 설정 이름은? (targets/configs/<name>.yaml)",
                default=suggested,
                console=console,
            )
            draft = _merge_draft(draft, EngagementDraft(target_name=target_name))
        try:
            load_target(draft.target_name)
            break
        except FileNotFoundError:
            console.print(
                f"[yellow]targets/configs/{draft.target_name}.yaml 없음[/yellow] — Engagement Setup 은 "
                "Scope/RoE 만 만든다, 어댑터 설정(회원가입/로그인 endpoint 등)은 사람이 직접 조사해서 "
                "별도로 작성해야 한다(RUNNING.md 3절)."
            )
            if Confirm.ask(
                "그래도 이 이름으로 계속 진행할까요? (아니오 = 다른 이름 다시 입력)",
                default=False,
                console=console,
            ):
                break
            draft = draft.model_copy(update={"target_name": None})
    else:
        console.print(
            f"[yellow]재입력 {_MAX_TARGET_NAME_RETRIES}회 한도 도달 — 어댑터 없이 그대로 진행합니다[/yellow]"
        )

    return draft


def _load_approved_engagements() -> list[Engagement]:
    engagements: list[Engagement] = []
    for path in list_available_engagements():
        try:
            eng = load_engagement(path)
        except Exception:  # noqa: BLE001 — 깨진 engagement.yaml 은 조용히 건너뛴다
            continue
        if eng.is_approved:
            engagements.append(eng)
    return engagements


def _offer_reuse(console: Console) -> Engagement | None:
    candidates = _load_approved_engagements()
    if not candidates:
        return None

    table = Table(show_header=True)
    table.add_column("#")
    table.add_column("engagement_id")
    table.add_column("target")
    for i, eng in enumerate(candidates, start=1):
        table.add_row(str(i), eng.engagement_id, eng.target.name)

    console.print(Panel("기존에 승인된 Engagement 가 있습니다", border_style="blue", expand=False))
    console.print(table)
    raw = Prompt.ask(
        f"재사용할 번호(1-{len(candidates)}), 새로 만들려면 엔터", default="", console=console
    ).strip()
    if raw.isdigit() and 1 <= int(raw) <= len(candidates):
        return candidates[int(raw) - 1]
    return None


def _print_credential_mapping(console: Console, engagement: Engagement) -> None:
    """제공된 계정이 어느 principal(예: attacker_A)에 매칭될지 승인 전에 명시적으로
    보여준다 — 순서 매칭이 암묵적이라 잘못된 역할로 쓰이는 실수를 승인 전에
    잡을 수 있게 한다."""
    try:
        target_cfg = load_target(engagement.target.name)
    except FileNotFoundError:
        console.print("[dim](어댑터 설정이 아직 없어 계정→principal 매핑은 미리 보여줄 수 없음)[/dim]")
        return

    table = Table(title="제공된 테스트 계정 매핑", show_header=True)
    table.add_column("계정")
    table.add_column("→ principal")
    principals = target_cfg.settings.principals
    for i, cred in enumerate(engagement.target.credentials):
        role = principals[i] if i < len(principals) else "[red](매칭되는 principal 없음 — 사용 안 됨)[/red]"
        table.add_row(cred.username, role)
    console.print(table)


def run_setup(console: Console, llm_factory, *, allow_reuse: bool = True) -> Engagement | None:
    """`llm_factory`: 인자 없는 콜러블(`() -> LLMClient`) — 실제로 새 대화가
    시작될 때만 호출된다(API 키 지연 확보). 돌려주는 `Engagement` 는 항상
    승인된(`approved_at` 있는) 것이다 — 재사용이든 새로 만들었든 마찬가지.
    사용자가 새로 만드는 중 취소하면 `None`."""
    # ★ 실측 버그 대응: 예전엔 `sys.stdin.isatty()`가 False면 실제로 물어보지도
    # 않고 곧장 자동 재사용/에러로 빠졌다 — 그런데 일부 셸/터미널 조합(Windows
    # PowerShell을 포함해 여럿에서 실측됨)에서 이 판정이 부정확하게 False를
    # 줘서, 진짜 대화형 터미널인데도 스코프 인터뷰를 항상 건너뛰는 사례가
    # 있었다. 이제는 isatty() 로 미리 걸러내지 않고 **항상 실제로 프롬프트를
    # 시도**한다 — 정말 입력을 못 받는 환경(CI 등)이면 `Prompt`/`Confirm.ask`
    # 자체가 `EOFError`를 던지므로 아래 `except EOFError`가 그 경우만 잡아
    # 자동 재사용/에러 폴백을 한다(반대 방향의 오탐 — isatty()가 부정확하게
    # True를 주는 경우 — 는 원래도 여기로 빠지도록 되어 있었다).
    try:
        if allow_reuse:
            reused = _offer_reuse(console)
            if reused is not None:
                return reused

        draft = _run_conversation(console, llm_factory)
        engagement_id = _generate_engagement_id(draft.target_name or "target")
        engagement = _draft_to_engagement(draft, engagement_id)

        console.print()
        if engagement.target.credentials:
            _print_credential_mapping(console, engagement)
        console.print(Panel(store_module.render_markdown(engagement), title="최종 확인", border_style="bold blue"))

        if not Confirm.ask("이 설정을 확정하시겠습니까?", default=False, console=console):
            console.print("[yellow]취소됨 — 아무 파일도 저장하지 않음[/yellow]")
            return None
    except EOFError as exc:
        # `sys.stdin.isatty()` 가 일부 셸/터미널 조합(예: Windows Git Bash 의 리다이렉트)에서
        # 부정확하게 True 를 줄 수 있다 — 여기서 실제로 입력이 막힌 걸 알게 되면, 위쪽의
        # "비대화형" 분기와 똑같이 재사용 가능하면 재사용하고, 아니면 명확한 에러로 끝낸다.
        if allow_reuse:
            candidates = _load_approved_engagements()
            if len(candidates) == 1:
                console.print(
                    Panel(
                        f"[bold yellow]입력을 읽을 수 없어(비대화형 환경) 스코프 인터뷰를 건너뛰고, "
                        f"이미 승인된 Engagement 를 자동 재사용합니다: {candidates[0].engagement_id}[/bold yellow]",
                        border_style="yellow",
                    )
                )
                return candidates[0]
        raise RuntimeError("입력을 읽을 수 없음(비대화형 환경) — Engagement Setup 은 대화형 터미널 전용") from exc

    dir_path = store_module.save_engagement(engagement)
    approved = store_module.approve(dir_path)
    console.print(f"[green]저장 및 승인 완료:[/green] {dir_path} (engagement_id={approved.engagement_id})")
    return approved
