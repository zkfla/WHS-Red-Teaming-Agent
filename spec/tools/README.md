# tools/ — 레거시 17개 ⚠️ 최신 tool 목록이 아니다

기존 코드(`finredteam/probing/*`)의 프로브를 매니페스트 양식으로 옮긴 것이다.
**새로 짤 때 쓰는 건 `../primitives/` 쪽이다.**

그런데도 남겨둔 이유가 둘 있다.

**1. 규격이 실물을 담을 수 있다는 증명.** 예제 0개짜리 스키마는 아무도 안 믿는다.
17개를 실제로 채워보는 과정에서 검사 규칙 ⑤~⑭ 가 나왔다.

**2. 대상 엔드포인트 목록.** 각 파일의 `surface` 는 실제 소스에서 뽑은 검증된 경로다.
아키텍처가 바뀌어도 이 정찰 결과는 그대로 쓴다.

```yaml
# 예: card_fund.yaml
surface:
  - {method: POST, path: "/api/virtual-cards/create"}
  - {method: POST, path: "/api/virtual-cards/{id}/fund", note: exchange_rate 주입 지점}
  - {method: GET,  path: "/api/virtual-cards", note: 카드 잔액 관측}
```

원시 동작으로 어떻게 접히는지는 `PYTHONPATH=. python spec/demo_primitives.py` 참고.

## 이 17개도 같은 규칙을 지킨다

레거시라고 검사가 느슨하지 않다. `discover_surface` 는 원래 `layer: meta` 로 표면
선언을 면제받고 있었는데(그 파일 주석이 "안전해서가 아니라 정적 선언이 불가능해서"
라고 스스로 적어뒀다), 검사 ⑨ 를 `offline` 기준으로 바꾸자 바로 걸렸다.
지금은 `surface_prefixes` 로 울타리를 선언한다.

`race_transfer` / `bill_race` 는 `race_testing` 을 선언하는데, 예전에는 그 권한을
검사하는 코드가 없어서 정책과 무관하게 그냥 돌았다. 지금은 `policy.check_manifest()`
가 막는다 — `PYTHONPATH=. python spec/demo_frontier.py` 의 [5] 를 보면 된다.
