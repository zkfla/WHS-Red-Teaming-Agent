"""engagement/setup.py 의 순수 로직 + 대화 루프.

실제 터미널 I/O(rich.Prompt/Confirm)는 이 모듈의 `Prompt`/`Confirm` 참조를
직접 스텁으로 바꿔서 우회한다 — TTY 없이도 대화 루프 전체를 결정론적으로
검증할 수 있다.
"""
from __future__ import annotations

import io
from decimal import Decimal

from rich.console import Console

from finredteam.engagement import setup as engagement_setup
from finredteam.engagement.setup import (
    EngagementDraft,
    _draft_to_engagement,
    _generate_engagement_id,
    _merge_draft,
    _slugify,
    run_setup,
)


def _console() -> Console:
    return Console(file=io.StringIO())


# ── 순수 함수 ──────────────────────────────────────────────────────


def test_slugify_strips_scheme_and_non_alnum():
    assert _slugify("http://localhost:5000") == "localhost-5000"


def test_slugify_empty_falls_back_to_target():
    assert _slugify("") == "target"


def test_generate_engagement_id_includes_slug_and_is_unique():
    a = _generate_engagement_id("vulnbank")
    b = _generate_engagement_id("vulnbank")
    assert a.startswith("vulnbank-")
    assert a != b  # 랜덤 접미사


def test_merge_draft_only_overwrites_non_none_fields():
    base = EngagementDraft(target_name="a", allow_state_change=True)
    update = EngagementDraft(base_url="http://x", allow_state_change=None)
    merged = _merge_draft(base, update)
    assert merged.target_name == "a"  # update 가 None 이라 안 덮어씀
    assert merged.base_url == "http://x"
    assert merged.allow_state_change is True  # update 의 None 이 기존 True 를 안 지움


def test_draft_to_engagement_defaults_unset_booleans_to_false():
    draft = EngagementDraft(target_name="t", base_url="http://localhost:5000")
    engagement = _draft_to_engagement(draft, "t-abc123")
    assert engagement.actions.allow_state_change is False
    assert engagement.actions.allow_account_creation is False
    assert engagement.actions.allow_dos is False
    assert engagement.data_access.allow_real_user_data is False


def test_draft_to_engagement_respects_explicit_true_values():
    draft = EngagementDraft(
        target_name="t", base_url="http://localhost:5000", allow_state_change=True, allow_account_creation=True
    )
    engagement = _draft_to_engagement(draft, "t-abc123")
    assert engagement.actions.allow_state_change is True
    assert engagement.actions.allow_account_creation is True
    assert engagement.actions.allow_dos is False  # 언급 안 된 건 여전히 False


def test_draft_to_engagement_derives_allowed_hosts_from_base_url_when_unset():
    draft = EngagementDraft(target_name="t", base_url="http://localhost:5000")
    engagement = _draft_to_engagement(draft, "t-abc123")
    assert engagement.scope.allowed_hosts == ["localhost"]


def test_draft_to_engagement_parses_max_amount_as_decimal():
    draft = EngagementDraft(target_name="t", base_url="http://x", max_amount="999.99")
    engagement = _draft_to_engagement(draft, "t-abc123")
    assert engagement.requests.max_amount == Decimal("999.99")


def test_draft_to_engagement_invalid_max_amount_falls_back_to_none():
    draft = EngagementDraft(target_name="t", base_url="http://x", max_amount="not-a-number")
    engagement = _draft_to_engagement(draft, "t-abc123")
    assert engagement.requests.max_amount is None


def test_draft_to_engagement_defaults_cleanup_required_true_when_unset():
    draft = EngagementDraft(target_name="t", base_url="http://x")
    engagement = _draft_to_engagement(draft, "t-abc123")
    assert engagement.cleanup.required is True  # 보수적 기본값 — 정리하는 쪽


def test_draft_to_engagement_carries_credentials_into_target():
    from finredteam.engagement.schema import EngagementCredential

    draft = EngagementDraft(
        target_name="t",
        base_url="http://localhost:5000",
        credentials=[EngagementCredential(username="alice", password="pw1")],
    )
    engagement = _draft_to_engagement(draft, "t-abc123")
    assert engagement.target.credentials == [EngagementCredential(username="alice", password="pw1")]


def test_draft_to_engagement_no_credentials_defaults_to_empty_list():
    draft = EngagementDraft(target_name="t", base_url="http://localhost:5000")
    engagement = _draft_to_engagement(draft, "t-abc123")
    assert engagement.target.credentials == []


# ── 대화 루프 (Prompt/Confirm 스텁으로 TTY 없이 검증) ──────────────


class _FakeSetupLLM:
    """실제 LLM 호출 없이 '한 번의 애매함 확인 후 종료'를 흉내낸다."""

    provider = "fake"
    model = "fake"

    def __init__(self) -> None:
        self.calls = 0

    def complete_json(self, system, user, schema, temperature=0.3):
        self.calls += 1
        if self.calls == 1:
            return [
                schema.model_validate(
                    {
                        "updated_fields": {"allow_state_change": True},
                        "next_question": "상태 변경 요청도 필요하신가요?",
                    }
                )
            ]
        return [schema.model_validate({"updated_fields": {}, "next_question": None})]


def _stub_prompts(monkeypatch, answers: list[str], confirm: bool = True):
    queue = list(answers)

    def _ask(question, default="", console=None, **kwargs):
        return queue.pop(0) if queue else default

    monkeypatch.setattr(engagement_setup.Prompt, "ask", staticmethod(_ask))
    monkeypatch.setattr(engagement_setup.Confirm, "ask", staticmethod(lambda *a, **k: confirm))


def _stub_sequenced_prompts(monkeypatch, prompt_answers: list[str], confirm_answers: list[bool]):
    """`_stub_prompts` 와 달리 Confirm.ask 도 호출 순서대로 다른 값을 낼 수 있게 한다
    — 어댑터 존재 확인(재입력 여부)과 최종 승인이 서로 다른 답이어야 하는 시나리오용."""
    p_queue = list(prompt_answers)
    c_queue = list(confirm_answers)

    def _ask(question, default="", console=None, **kwargs):
        return p_queue.pop(0) if p_queue else default

    def _confirm(question, default=False, console=None, **kwargs):
        return c_queue.pop(0) if c_queue else default

    monkeypatch.setattr(engagement_setup.Prompt, "ask", staticmethod(_ask))
    monkeypatch.setattr(engagement_setup.Confirm, "ask", staticmethod(_confirm))


def test_run_setup_new_engagement_full_flow(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    answers = [
        "",  # _offer_reuse: 재사용 목록 없음 -> 프롬프트 자체가 안 뜨지만 안전하게 큐에 둠
        "vuln-bank 로컬 인스턴스를 테스트하고 싶다",  # 초기 설명
        "필요하다",  # next_question 에 대한 답
    ]
    _stub_prompts(monkeypatch, answers, confirm=True)
    monkeypatch.setattr(engagement_setup.sys.stdin, "isatty", lambda: True)

    llm = _FakeSetupLLM()
    engagement = run_setup(_console(), lambda: llm, allow_reuse=False)

    assert engagement is not None
    assert engagement.is_approved
    assert engagement.actions.allow_state_change is True
    assert (tmp_path / "engagements" / engagement.engagement_id / "engagement.yaml").exists()
    assert (tmp_path / "engagements" / engagement.engagement_id / "engagement.md").exists()


def test_run_setup_still_prompts_when_isatty_misreports_false(tmp_path, monkeypatch):
    """실측 버그 회귀 가드: 일부 셸/터미널 조합에서 `sys.stdin.isatty()`가 실제로는
    대화형인데도 False를 잘못 줬다 — 그 경우 예전엔 승인된 Engagement가 하나라도
    있으면 묻지도 않고 조용히 그걸 재사용해버렸다. 이제는 isatty() 로 미리 걸러내지
    않고 항상 프롬프트를 실제로 시도해야 한다(입력이 실제로 막혀 있을 때만
    EOFError 로 자동 재사용 폴백을 탄다)."""
    from finredteam.engagement import store as store_module

    monkeypatch.chdir(tmp_path)
    existing = _draft_to_engagement(
        EngagementDraft(target_name="vulnbank", base_url="http://localhost:5000"), "vulnbank"
    )
    dir_path = store_module.save_engagement(existing)
    store_module.approve(dir_path)

    monkeypatch.setattr(engagement_setup.sys.stdin, "isatty", lambda: False)  # 오탐 재현
    prompt_answers = [
        "",  # _offer_reuse 프롬프트 — 빈 값 = "새로 만들기" 선택
        "새 대상을 테스트하고 싶다",  # 초기 설명
        "필요하다",  # LLM 의 유일한 clarify 질문에 대한 답
        "http://localhost:9999",  # base_url 직접 질문
        "newtarget",  # target_name 직접 질문 (어댑터 없음 → "계속 진행" 선택)
    ]
    confirm_answers = [True, True]  # 1) 어댑터 없어도 계속? Yes  2) 최종 확인? Yes
    _stub_sequenced_prompts(monkeypatch, prompt_answers, confirm_answers)

    llm = _FakeSetupLLM()
    engagement = run_setup(_console(), lambda: llm, allow_reuse=True)

    assert engagement is not None
    assert engagement.engagement_id != "vulnbank"  # 조용히 자동 재사용되지 않았음
    assert engagement.target.base_url == "http://localhost:9999"


def test_run_setup_cancelled_at_final_confirm_saves_nothing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    answers = ["테스트 대상 설명", "답"]
    _stub_prompts(monkeypatch, answers, confirm=False)
    monkeypatch.setattr(engagement_setup.sys.stdin, "isatty", lambda: True)

    llm = _FakeSetupLLM()
    engagement = run_setup(_console(), lambda: llm, allow_reuse=False)

    assert engagement is None
    assert not (tmp_path / "engagements").exists()


def test_run_setup_llm_factory_failure_falls_back_to_direct_questions(tmp_path, monkeypatch):
    """LLM 준비 자체가 실패해도(API 키 없음 등) base_url/target_name 을 직접
    물어서 최소한의 Engagement 는 만들 수 있어야 한다 — 전부 보수적 기본값으로."""
    monkeypatch.chdir(tmp_path)
    answers = [
        "설명",
        "http://localhost:9999",  # base_url 직접 질문
        "myapp",  # target_name 직접 질문
    ]
    _stub_prompts(monkeypatch, answers, confirm=True)
    monkeypatch.setattr(engagement_setup.sys.stdin, "isatty", lambda: True)

    def _broken_factory():
        raise RuntimeError("API 키 없음")

    engagement = run_setup(_console(), _broken_factory, allow_reuse=False)

    assert engagement is not None
    assert engagement.target.name == "myapp"
    assert engagement.target.base_url == "http://localhost:9999"
    assert engagement.actions.allow_state_change is False  # 질문을 못 했으니 보수적 기본값


# ── 어댑터(Legacy Target Adapter) 존재 조기 확인 ─────────────────────


def test_run_setup_target_name_missing_adapter_prompts_retry(tmp_path, monkeypatch):
    """어댑터가 없는 이름을 입력하면 전체 인터뷰가 끝날 때까지 기다리지 않고
    바로 알려주고 다시 물어본다 — "아니오(재입력)" 선택 시 실제로 재입력 루프가
    돈다. `vulnbank` 는 저장소에 실제로 커밋된 어댑터라 `load_target`이 cwd와
    무관하게 성공한다(§ targets/loader.py — 패키지 디렉터리 기준)."""
    monkeypatch.chdir(tmp_path)
    prompt_answers = [
        "설명",  # 초기 설명
        "답",  # LLM 의 유일한 clarify 질문에 대한 답
        "http://localhost:5000",  # base_url (LLM 이 못 정해서 직접 질문)
        "totally-fake-adapter-name",  # target_name 1차 시도 — 어댑터 없음
        "vulnbank",  # target_name 2차 시도 — 실제 존재
    ]
    confirm_answers = [False, True]  # 1) 그래도 계속? No(재입력)  2) 최종 확인? Yes
    _stub_sequenced_prompts(monkeypatch, prompt_answers, confirm_answers)
    monkeypatch.setattr(engagement_setup.sys.stdin, "isatty", lambda: True)

    llm = _FakeSetupLLM()
    engagement = run_setup(_console(), lambda: llm, allow_reuse=False)

    assert engagement is not None
    assert engagement.target.name == "vulnbank"


def test_run_setup_target_name_missing_adapter_user_confirms_continue_anyway(tmp_path, monkeypatch):
    """"그래도 계속할까요?" 에 예라고 답하면, 어댑터가 없어도 그 이름 그대로
    Engagement 는 저장된다(실행 자체는 나중에 __main__.py 의 늦은 체크가 막는다
    — 이 테스트는 그 안전망까지는 확인하지 않는다)."""
    monkeypatch.chdir(tmp_path)
    prompt_answers = ["설명", "답", "http://localhost:5000", "totally-fake-adapter-name"]
    confirm_answers = [True, True]  # 1) 그래도 계속? Yes  2) 최종 확인? Yes
    _stub_sequenced_prompts(monkeypatch, prompt_answers, confirm_answers)
    monkeypatch.setattr(engagement_setup.sys.stdin, "isatty", lambda: True)

    llm = _FakeSetupLLM()
    engagement = run_setup(_console(), lambda: llm, allow_reuse=False)

    assert engagement is not None
    assert engagement.target.name == "totally-fake-adapter-name"
