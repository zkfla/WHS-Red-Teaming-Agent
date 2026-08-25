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
