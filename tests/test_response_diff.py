import httpx

from whs_redteam.tools.http_client import SafeHttpResponse
from whs_redteam.tools.response_diff import compare_responses, fingerprint


def make_response(body: bytes, content_type: str = "application/json") -> SafeHttpResponse:
    return SafeHttpResponse(
        url="https://example.test/api",
        status_code=200,
        headers=httpx.Headers({"content-type": content_type}),
        body=body,
        elapsed_ms=1.0,
    )


def test_json_fingerprint_stores_shape_not_values() -> None:
    result = fingerprint(make_response(b'{"email":"private@example.test","items":[{"id":1}]}'))
    assert result.json_shape == {"email": "str", "items": [{"id": "int"}]}
    assert "private@example.test" not in str(result.json_shape)


def test_response_diff_detects_shape_change() -> None:
    first = make_response(b'{"items":[{"id":1}]}')
    second = make_response(b'{"items":[{"id":1,"role":"admin"}]}')
    result = compare_responses(first, second)
    assert result["body_equal"] is False
    assert result["json_shape_equal"] is False
