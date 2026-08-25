"""storage/evidence.py 단위 테스트 — §18 조건 14: 민감 응답 원문이 저장되지 않는다."""
from __future__ import annotations

from finredteam.storage.evidence import CapturedResponse, store_response


def test_critical_fields_are_redacted_not_stored():
    resp = CapturedResponse(
        status_code=200,
        body={"card_number": "4111111111111111", "cvv": "123", "status": "success"},
        text='{"card_number": "4111111111111111", "cvv": "123", "status": "success"}',
    )
    ref = store_response(resp, context={})

    assert ref.mode == "redacted"
    assert ref.body is None  # 원문 폐기
    assert ref.field_presence is not None
    assert ref.field_presence["card_number"] is True
    assert ref.field_presence["cvv"] is True
    # 스키마에는 타입만 남고 값은 없다 — "card_number" 문자열 원문이 스키마 안에 없어야 한다.
    assert ref.value_schema is not None
    assert "4111111111111111" not in str(ref.value_schema)


def test_low_sensitivity_fields_kept_in_full_mode():
    resp = CapturedResponse(
        status_code=200,
        body={"status": "success", "message": "ok"},
        text='{"status": "success", "message": "ok"}',
    )
    ref = store_response(resp, context={})

    assert ref.mode == "full"
    assert ref.body is not None
    assert "success" in ref.body


def test_body_hash_is_deterministic_for_same_content():
    resp_a = CapturedResponse(status_code=200, body={"a": 1, "b": 2}, text="{}")
    resp_b = CapturedResponse(status_code=200, body={"b": 2, "a": 1}, text="{}")  # 키 순서만 다름
    ref_a = store_response(resp_a, context={})
    ref_b = store_response(resp_b, context={})
    assert ref_a.body_hash == ref_b.body_hash  # canonical 정렬 덕에 순서 무관하게 동일


def test_f8_enumeration_response_redacts_other_principals_balance():
    # F8 열거 프로브가 타인의 잔액·이름을 실제로 받아왔다고 해도 원문을 남기지 않는다.
    resp = CapturedResponse(
        status_code=200,
        body={"account_number": "1234567890", "balance": 58213.44, "username": "victim_V"},
        text='{"account_number": "1234567890", "balance": 58213.44, "username": "victim_V"}',
    )
    ref = store_response(resp, context={"probe": "F8-enum"})

    assert ref.mode == "redacted"
    assert ref.body is None
    assert "58213.44" not in str(ref.value_schema)
    assert "victim_V" not in str(ref.value_schema)
