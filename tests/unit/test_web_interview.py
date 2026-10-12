"""LLM-led interview: the model proposes, code keeps only what the operator actually said."""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

from redteam.app.web.engagement_catalog import EngagementCatalog
from redteam.app.web.host import SessionHost
from redteam.app.web.interview import (
    MAX_SESSIONS,
    MAX_TURNS,
    InterviewError,
    InterviewService,
)
from redteam.app.web.security import CLIENT_HEADER, GuardPolicy
from redteam.app.web.server import create_app
from redteam.app.web.settings import SettingsStore
from redteam.core.ports.llm import LLMProviderFailure
from redteam.intake.models import EngagementDraft
from redteam.llm.fake import FakeLLMClient

PORT = 8787
BASE = f"http://127.0.0.1:{PORT}"
HEADERS = {"Origin": BASE, "Content-Type": "application/json", CLIENT_HEADER: "web"}


def reply(draft=None, ack="확인했습니다.", questions=None):
    return {
        "draft": draft or {},
        "acknowledgement": ack,
        "follow_up_questions": questions or [],
        "ready": False,
    }


def service(*responses, clock=None):
    llm = FakeLLMClient(structured_responses=list(responses))
    kwargs = {} if clock is None else {"clock": clock}
    return InterviewService(lambda: llm, **kwargs), llm


async def test_turns_accumulate_a_draft_and_completeness_is_decided_by_code():
    svc, _ = service(
        reply({"name": "lab", "allowed_hosts": ["localhost:8081"]}, questions=["예산은?"]),
        reply({"max_actions": 7}),
    )
    first = await svc.turn(session_id=None, message="localhost:8081 의 lab을 점검")
    assert first["draft"]["allowed_hosts"] == ["localhost:8081"]
    assert first["complete"] is False and first["missing"] == ["max_actions (execution budget)"]
    assert first["questions"] == ["예산은?"]
    second = await svc.turn(session_id=first["session_id"], message="최대 7번")
    assert second["complete"] is True and second["draft"]["name"] == "lab"
    assert second["draft"]["allowed_hosts"] == ["localhost:8081"]  # earlier answers kept


async def test_scope_the_operator_never_typed_is_dropped_and_reported():
    svc, _ = service(
        reply(
            {
                "name": "lab",
                "allowed_hosts": ["localhost:8081", "localhost:9999", "evil.example"],
                "allowed_domains": ["example.com"],
                "allowed_networks": ["10.0.0.0/8"],
            }
        )
    )
    result = await svc.turn(
        session_id=None, message="http://localhost:8081/login 점검, 도메인 sub.example.com"
    )
    assert result["draft"]["allowed_hosts"] == ["localhost:8081"]
    assert result["draft"]["allowed_domains"] == []  # "example.com" is wider than what was typed
    assert result["draft"]["allowed_networks"] == []
    codes = {(r["field"], r["code"]) for r in result["rejected"]}
    assert codes == {
        ("allowed_hosts", "not_stated_by_operator"),
        ("allowed_domains", "not_stated_by_operator"),
        ("allowed_networks", "not_stated_by_operator"),
    }
    assert len([r for r in result["rejected"] if r["field"] == "allowed_hosts"]) == 2


async def test_a_port_the_operator_did_not_write_is_not_added_to_a_host():
    svc, _ = service(reply({"allowed_hosts": ["localhost:8081"]}))
    result = await svc.turn(session_id=None, message="localhost 를 점검")
    assert result["draft"]["allowed_hosts"] == []
    assert result["rejected"] == [{"field": "allowed_hosts", "code": "not_stated_by_operator"}]


async def test_domain_and_network_typed_exactly_are_accepted():
    svc, _ = service(
        reply({"allowed_domains": ["example.com"], "allowed_networks": ["10.0.0.0/24"]})
    )
    result = await svc.turn(session_id=None, message="도메인 example.com, 네트워크 10.0.0.0/24")
    assert result["draft"]["allowed_domains"] == ["example.com"]
    assert result["draft"]["allowed_networks"] == ["10.0.0.0/24"]
    assert result["rejected"] == []


async def test_invalid_capabilities_and_counts_are_rejected_not_saved():
    svc, _ = service(
        reply({"allowed_capabilities": ["web", "root"], "max_actions": -4, "name": "ok"})
    )
    result = await svc.turn(session_id=None, message="점검")
    assert result["draft"]["allowed_capabilities"] == []
    assert result["draft"]["max_actions"] is None and result["draft"]["name"] == "ok"
    assert {r["field"] for r in result["rejected"]} == {"allowed_capabilities", "max_actions"}


async def test_scope_already_in_the_starting_draft_is_kept_when_editing():
    base = EngagementDraft(name="lab", allowed_hosts=["localhost:8081"], max_actions=5)
    svc, _ = service(reply({"notes": ["야간 금지"]}))
    result = await svc.turn(session_id=None, message="야간 금지 추가", base=base, editing=True)
    assert result["draft"]["allowed_hosts"] == ["localhost:8081"]
    assert result["draft"]["notes"] == ["야간 금지"] and result["rejected"] == []


async def test_web_path_choice_and_https_origin_are_preserved_through_chat_edit():
    from redteam.app.web.engagement import parse_draft

    base = parse_draft({"name": "lab", "allowed_hosts": ["https://localhost:8443"],
                        "web_policy_mode": "paths", "web_paths": ["/login"], "max_actions": 5})
    svc, _ = service(reply({"notes": ["야간 금지"]}))
    result = await svc.turn(session_id=None, message="야간 금지 추가", base=base, editing=True)
    assert result["draft"]["web_policy_mode"] == "paths"
    assert result["draft"]["web_paths"] == ["/login"]
    assert result["target_origins"] == {"localhost:8443": "https://localhost:8443"}


async def test_secrets_are_not_sent_to_the_model_and_model_text_is_masked():
    svc, llm = service(reply(ack="password=hunter2-secret-value 저장"))
    result = await svc.turn(session_id=None, message="api_key=sk-test-secret-value-123456 입니다")
    assert "sk-test-secret-value-123456" not in json.dumps(llm.requests)
    assert "hunter2-secret-value" not in result["acknowledgement"]


async def test_limits_expiry_and_failures_are_explicit():
    now = [0.0]
    svc, _ = service(clock=lambda: now[0])
    for bad in (None, "", "   ", 5):
        with pytest.raises(InterviewError) as raised:
            await svc.turn(session_id=None, message=bad)
        assert raised.value.code == "message_required"
    with pytest.raises(InterviewError) as raised:
        await svc.turn(session_id=None, message="x" * 2001)
    assert raised.value.code == "message_too_long"
    with pytest.raises(InterviewError) as raised:
        await svc.turn(session_id="nope", message="hi")
    assert raised.value.code == "session_expired"

    svc, _ = service(*[reply() for _ in range(MAX_TURNS + 1)], clock=lambda: now[0])
    first = await svc.turn(session_id=None, message="시작")
    for _ in range(MAX_TURNS - 1):
        await svc.turn(session_id=first["session_id"], message="계속")
    with pytest.raises(InterviewError) as raised:
        await svc.turn(session_id=first["session_id"], message="또")
    assert raised.value.code == "turn_limit"
    now[0] += 4000
    with pytest.raises(InterviewError) as raised:
        await svc.turn(session_id=first["session_id"], message="늦음")
    assert raised.value.code == "session_expired"


async def test_a_broken_model_reply_is_reported_and_leaves_the_text_out_of_scope_support():
    svc, _ = service({"draft": {"allowed_hosts": ["a@b"]}}, reply({"allowed_hosts": ["evil:1"]}))
    with pytest.raises(InterviewError) as raised:
        await svc.turn(session_id=None, message="evil:1 은 아직 허가 안 됨")
    assert raised.value.code == "assistant_failed" and raised.value.detail == "invalid_response"


async def test_provider_failures_surface_a_structured_diagnostic_only():
    class Failing:
        async def complete_structured(self, **kwargs):
            raise LLMProviderFailure(kind="auth", http_status=401, provider_code="bad_key")

    svc = InterviewService(lambda: Failing())
    with pytest.raises(InterviewError) as raised:
        await svc.turn(session_id=None, message="점검")
    assert raised.value.code == "assistant_failed"
    assert raised.value.detail == "auth http=401 code=bad_key"


async def test_old_sessions_are_evicted_beyond_the_cap():
    svc, _ = service(*[reply() for _ in range(MAX_SESSIONS + 1)])
    first = await svc.turn(session_id=None, message="하나")
    for _ in range(MAX_SESSIONS):
        await svc.turn(session_id=None, message="다른 대화")
    with pytest.raises(InterviewError) as raised:
        await svc.turn(session_id=first["session_id"], message="처음 대화")
    assert raised.value.code == "session_expired"


@pytest.fixture
async def api(tmp_path):
    settings = SettingsStore(
        env={}, state_path=tmp_path / "w.json", profile_dir=tmp_path, env_path=tmp_path / ".env"
    )
    catalog = EngagementCatalog(tmp_path / "engagements")
    launcher = SimpleNamespace(build_config=lambda: None, launch=None)
    host = SessionHost(settings=settings, launcher=launcher, catalog=catalog)
    svc, llm = service(
        reply({"name": "lab", "allowed_hosts": ["localhost:8081"], "max_actions": 6}),
    )
    app = create_app(host, GuardPolicy.for_loopback(PORT), svc)
    plain = create_app(host, GuardPolicy.for_loopback(PORT))  # default factory, no LLM settings
    out = SimpleNamespace(host=host, catalog=catalog, llm=llm, tmp=tmp_path)
    for name, application in (("client", app), ("plain", plain)):
        transport = httpx.ASGITransport(app=application, client=("127.0.0.1", 50000))
        setattr(out, name, httpx.AsyncClient(transport=transport, base_url=BASE))
    yield out
    await out.client.aclose()
    await out.plain.aclose()


async def post(client, body, path="/api/v1/engagement/interview"):
    return await client.post(path, content=json.dumps(body), headers=HEADERS)


async def test_api_turn_returns_a_draft_and_never_writes_a_file(api):
    response = await post(api.client, {"message": "localhost:8081 lab 점검, 6번"})
    assert response.status_code == 200
    body = response.json()
    assert body["complete"] is True and body["draft"]["name"] == "lab"
    assert not (api.tmp / "engagements").exists()
    # The reviewed draft then goes through the normal preview, which needs no model.
    preview = await post(api.client, {"draft": body["draft"]}, "/api/v1/engagement/preview")
    assert preview.status_code == 200 and preview.json()["summary"]["name"] == "lab"


async def test_api_errors_map_to_distinct_statuses(api):
    missing_settings = await post(api.plain, {"message": "hi"})
    assert missing_settings.status_code == 409
    assert missing_settings.json() == {"error": "settings_required"}
    assert (await post(api.client, {"message": ""})).status_code == 400
    assert (await post(api.client, {"session_id": "x", "message": "hi"})).status_code == 404
    assert (await post(api.client, {"id": "../x", "message": "hi"})).status_code == 404
    bad_draft = await post(api.client, {"message": "hi", "draft": {"zzz": 1}})
    assert bad_draft.status_code == 422
    assert bad_draft.json()["error"] == "invalid_engagement"


async def test_api_edit_starts_from_the_saved_file_and_refuses_hand_written_ones(api):
    saved = api.catalog.store("lab", must_exist=False).save(
        {"name": "lab", "allowed_hosts": ["localhost:8081"], "max_actions": 4},
        confirm_scope=True, confirm_expansion=False,
    )
    assert saved["saved"]
    response = await post(api.client, {"id": "lab", "message": "특이사항 없음"})
    assert response.status_code == 200
    assert response.json()["draft"]["allowed_hosts"] == ["localhost:8081"]
    (api.tmp / "engagements" / "hand.yaml").write_text(
        "name: h\nscope:\n  allowed_hosts: [localhost]\n"
        "rules_of_engagement:\n  - name: a\n  - name: b\nexecution_budget:\n  max_actions: 2\n",
        encoding="utf-8",
    )
    refused = await post(api.client, {"id": "hand", "message": "수정"})
    assert refused.status_code == 409 and refused.json() == {"error": "not_managed"}


async def test_api_interview_requires_the_page_proofs(api):
    response = await api.client.post(
        "/api/v1/engagement/interview",
        content=json.dumps({"message": "hi"}),
        headers={**HEADERS, "Origin": "http://evil.example"},
    )
    assert response.status_code == 403
