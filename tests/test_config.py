"""config.py 안전장치 단위 테스트 — §18 조건 17: allowlist 밖 호스트는 즉시 실패.

`allowed_hosts` 는 이제 호출부(engagement/policy.py)가 명시적으로 넘긴다 —
여기선 vuln-bank 예시와 동일한 집합을 직접 넘겨서 저수준 로직만 검증한다.
"""
from __future__ import annotations

import pytest

from finredteam.config import AllowedHostError, assert_allowed_host

_HOSTS = {"localhost", "127.0.0.1"}


def test_allowed_hosts_pass():
    assert_allowed_host("http://localhost:5000/transfer", _HOSTS)
    assert_allowed_host("http://127.0.0.1:5000/check_balance/1", _HOSTS)


def test_disallowed_host_raises_immediately():
    with pytest.raises(AllowedHostError):
        assert_allowed_host("http://evil.example.com/steal", _HOSTS)


def test_disallowed_host_with_similar_looking_domain_raises():
    # "localhost.evil.com" 같은 편법도 막혀야 한다 — hostname 전체 비교이지 부분일치가 아니다.
    with pytest.raises(AllowedHostError):
        assert_allowed_host("http://localhost.evil.com/transfer", _HOSTS)


def test_empty_allowed_hosts_blocks_everything():
    with pytest.raises(AllowedHostError):
        assert_allowed_host("http://localhost:5000/transfer", set())
