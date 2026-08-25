"""생성 리소스 추적 + CLEANUP phase (engagement §11).

target adapter 에 정리 방법(`cleanup_ops`)이 없으면 조용히 넘어가지 않고
"not_configured" 로 정직하게 기록되는지, 정의돼 있으면 실제로 실행해서
성공/실패를 기록하는지 확인한다."""
from __future__ import annotations

from finredteam.engagement.cleanup import CreatedResource, run_cleanup
from finredteam.models.core import Principal


class _StubTarget:
    def __init__(self, ops: dict) -> None:
        self._ops = ops

    def cleanup_ops(self) -> dict:
        return self._ops


class _StubExchange:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


class _StubSession:
    def __init__(self, status_code: int) -> None:
        self._status_code = status_code
        self.calls: list[tuple[str, str]] = []

    def authed_request(self, method, path, principal, json_body=None):
        self.calls.append((method, path))
        return _StubExchange(self._status_code)

    def anonymous_request(self, method, path, json_body=None):
        self.calls.append((method, path))
        return _StubExchange(self._status_code)


def _principal(pid: str) -> Principal:
    return Principal(id=pid, username=f"{pid}_u", password="pw", token="tok")


def test_resource_without_configured_cleanup_op_is_recorded_honestly():
    target = _StubTarget(ops={})
    session = _StubSession(status_code=200)
    resources = [CreatedResource(resource_type="account", resource_id="acc-1", created_by_step=0, principal_id="a")]

    records = run_cleanup(session, target, resources, {"a": _principal("a")})

    assert len(records) == 1
    assert records[0].cleanup_status == "not_configured"
    assert not session.calls  # 정의된 게 없으니 요청 자체를 시도하지 않아야 함


def test_resource_with_configured_cleanup_op_succeeds():
    target = _StubTarget(ops={"account": {"method": "DELETE", "path": "/accounts/{resource_id}"}})
    session = _StubSession(status_code=204)
    resources = [CreatedResource(resource_type="account", resource_id="acc-1", created_by_step=0, principal_id="a")]

    records = run_cleanup(session, target, resources, {"a": _principal("a")})

    assert records[0].cleanup_status == "success"
    assert session.calls == [("DELETE", "/accounts/acc-1")]


def test_resource_with_configured_cleanup_op_records_failure_status():
    target = _StubTarget(ops={"account": {"method": "DELETE", "path": "/accounts/{resource_id}"}})
    session = _StubSession(status_code=500)
    resources = [CreatedResource(resource_type="account", resource_id="acc-1", created_by_step=0, principal_id="a")]

    records = run_cleanup(session, target, resources, {"a": _principal("a")})

    assert records[0].cleanup_status == "failed"


def test_multiple_resource_types_mixed_configured_and_not():
    target = _StubTarget(ops={"account": {"method": "DELETE", "path": "/accounts/{resource_id}"}})
    session = _StubSession(status_code=204)
    resources = [
        CreatedResource(resource_type="account", resource_id="acc-1", created_by_step=0, principal_id="a"),
        CreatedResource(resource_type="post", resource_id="post-1", created_by_step=1, principal_id="a"),
    ]

    records = run_cleanup(session, target, resources, {"a": _principal("a")})

    by_type = {r.resource_type: r.cleanup_status for r in records}
    assert by_type == {"account": "success", "post": "not_configured"}
