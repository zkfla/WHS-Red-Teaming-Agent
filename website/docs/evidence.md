# 증거와 결과 해석

행동과 관측이 쌓였으니, 이제 그것을 어떻게 결론(Finding)으로 바꾸고 무엇을 "확인됨"으로 볼지 다룬다. 이 프레임워크가 가장 조심하는 지점이다.

증거는 append-only `EvidenceStore`에 쌓이고 원본은 보존하며, 큰 바이너리는 `artifact_ref`로 참조한다. `action_id`, `invocation_id`, `hypothesis_id`로 상관되어 어떤 행동과 관측이 어떤 가설을 뒷받침하는지 추적된다. 시스템은 다음을 명확히 구분한다.

| 구분 | 의미 |
| --- | --- |
| 도구 실행 성공 | 호출이 수행되어 결과를 반환했는가 |
| 검증 작업 결과 | 이번 Probe의 목적에 대해 무엇을 확인했는가 (confirmed, refuted, inconclusive, blocked) |
| 취약점 판단 | 관측이 취약점 후보를 뒷받침하거나 반박하는가 (Finding.verified) |
| 목표 달성 | 전체 점검의 완료 조건을 충족했는가 |

판단 불가나 차단된 결과도 관측과 한계를 남기며 그것이 취약점 확인으로 바뀌지 않는다. 민감 본문은 기존 마스킹(`core.redaction`)을 재사용한다.

## Findings와 보고

검증된 결론은 `Finding`으로 공격 그래프에 반영되고, `FindingCreated`와 `FindingVerified` 이벤트로 흐른다. `Finding.verified`가 True이고 `evidence_ids`가 원본 증거를 가리킬 때만 확인된 취약점으로 다룬다. 보고 기능(`report generate`)은 현재 저장된 상태를 근거로 진행 상황과 확인된 결과, 미해결 사항을 보여준다. 코드 산출물도 같은 원칙을 따른다. 코드 작성, 제한된 시험 환경에서의 검증, 실제 대상에서의 효과 확인은 서로 다른 단계로 취급한다.

## 보안 모델과 신뢰 경계

- 외부 입력은 데이터다. 웹, 파일, 도구, 검색 결과는 데이터일 뿐, 시스템 프롬프트나 정책, 실행 명령으로 격상되지 않는다(데이터를 지시로 해석하지 않는다).
- 등록은 실행 권한이 아니다. 역할에 능력이 있거나 도구가 카탈로그에 있다고 실행이 허가되지 않는다. 실제 도구 준비 상태와 해당 점검의 허용 조건이 충족돼야 한다.
- 다섯 경계를 구분한다. 가용성, 선택, admission, 실제 실행, 결과 해석. 성공이나 거절, mock, 코드 생성만으로 성공을 확정하지 않는다.
- 가용성 영향은 `ActionProposal.availability_impact`를 에이전트가 선언해야 `RuleOfEngagement.availability_impact_allowed`가 작동한다.
- Scope는 허용 목록이 없으면 거절이 기본값이다.

## 설계 결정 (ADR 요약)

- LangGraph는 워크플로 런타임 전용이고 상태는 ID만 담는다. (`docs/adr/0001`)
- LiteLLM 게이트웨이는 선택 extra로 지연 import하여, 기본 패키지와 `FakeLLMClient`가 무거운 의존 없이 import된다. (`docs/adr/0002`)
- MCP는 Capability 어댑터 패턴이다. 도구 빌드가 아니라 연결이다. (`docs/adr/0003`)
- In-memory와 Filesystem 백엔드를 포트 뒤에 두고 Redis, NATS, Kafka, Neo4j는 의도적으로 보류한다. 추가해도 호출부가 바뀌지 않도록 설계했다.

## 검증 전략

- 단위와 통합 시험에 더해 구조 테스트를 둔다. `test_control_plane_bypass.py`가 `orchestration`의 Capability, Control import 부재를 정적으로 검사한다.
- fake와 real을 구분한다. 고정 모델 응답이나 합성 도구 시험은 상태 처리와 구성 연결을 확인하지만 실제 탐지 정확도나 성능을 입증하지 않는다. 구현 검증과 성능 평가는 다르다.
- 라이브 호출(실제 MCP, LLM, 대상)은 요청과 승인 범위 안에서만 한다. `validate`는 대상에 접촉하지 않고 설정과 정책만 검사한다.
- 실제 환경 평가 지표는 목표 달성 여부에 더해 잘못된 판단, 미해결 항목, 모델 호출량, 실행 시간, 복구 동작이다.
