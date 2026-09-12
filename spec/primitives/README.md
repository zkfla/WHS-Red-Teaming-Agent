# primitives/ — 원시 동작 11개 ★ 이쪽이 최신

새 아키텍처에서 쓰는 tool 어휘. 레시피가 아니라 **조립 부품**이다.

레거시 `../tools/` 의 17개는 이 11개 조합으로 전부 재현된다
(`PYTHONPATH=. python spec/demo_primitives.py` 로 확인).

`card_limit` · `loan_limit` · `bill_limit` 은 셋 다 "어떤 주소에 이상한 값 넣기"
하나였다 — 레시피라서 따로였을 뿐이다.

| 파일 | 하는 일 |
|---|---|
| `probe_path` | 모르는 경로를 읽기 전용으로 찔러본다 — `surface_prefixes` 울타리 안에서만 |
| `http_request` | **발견한** 엔드포인트에 요청을 보낸다 — 레거시 17개 중 11개가 여기로 |
| `observe_state` | 잔액을 읽어 델타의 기준을 만든다 |
| `concurrent_burst` | 같은 요청을 동시에 N번 (경쟁조건) |
| `repeat_request` | 같은 요청을 순차로 N번 (멱등성·rate limit) |
| `create_principal` | 가입. 추가 필드 주입 지점을 겸한다 |
| `authenticate` | 로그인해 토큰 획득 |
| `forge_token` | 토큰 위조 — `offline: true` (네트워크를 타지 않는다) |
| `delete_principal` `delete_card` `cancel_loan` | 정리 전용. `mcp.expose: false` — 모델이 아니라 control plane 이 부른다 |

## 전제를 params 와 묶는다

동적 대상 tool(`surface_binding` 을 쓰는 것들)은 전제에 **반드시** `from_param` 이
있어야 한다.

```yaml
surface_binding: target
preconditions:
  - {kind: endpoint, attrs: {reachable: true}, bind: target, from_param: target}
params:
  target: {type: string, required: true, binds_to: endpoint}
```

`binds_to` 는 *종류*("endpoint 노드여야 한다")만, `from_param` 은 *동일성*("모델이
고른 바로 그 노드")을 말한다. 동일성 선언이 빠지면 `resolve()` 가 패턴에 맞는 아무
노드나 집고, `ingest()` 가 그 엉뚱한 바인딩을 허용 표면으로 삼는다 — 스코프 이탈
탐지가 통째로 틀린 기준으로 돈다. 검사기 ⑳ 가 CI 에서 막는다.

자격증명·자유 입력 파라미터(`password`, `body`, `extra_fields`, `claims`)에는
`sensitive: true` 를 붙인다. 기록에는 «redacted» 로 남는다(검사기 ㉑).

읽는 법은 `../tool_manifest.md` 참고.
