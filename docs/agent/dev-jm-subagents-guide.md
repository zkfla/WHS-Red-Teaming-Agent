# dev-jm 서브에이전트 구성과 구현 안내

기준: 2026-10-08 로컬 검수 완료 코드. 이 문서는 PRD의 희망 구조가 아니라
`config/agents/specialists-p2.yaml` 및 실제 조립·위임·실행 코드를 설명한다.

## 1. 먼저 이해할 구조

현재 P2 선택 프로필은 **7개 역할**을 등록한다. 대상에 도구를 실행하도록
스케줄링되는 역할은 **Web 실행과 Mobile 실행 두 개**다. 나머지 역할은 대화,
계획, 작업 분해, 결과 검토, 코드 산출물의 단계별 모델 판단을 담당한다.

각 역할이 별도 서버·OS 프로세스·상시 실행 모델을 하나씩 갖는 구조는 아니다.
같은 런타임에서 역할별 시스템 지침·schema·문맥을 적용해 LLM을 호출하고,
위임된 조사 상태와 실제 작업 원장을 보존한다. P2의 병렬 판단에는 요청별
스냅샷·격리된 판단 상태를 사용하며 실행은 기존 MCP 서버로 전달한다.

역할은 분업하지만 **실행과 최종 상태 반영을 조율하는 Engine/Runner는 남아 있다.**
완전히 독립된 동등한 에이전트가 서로 직접 메시지를 주고받는 분산 시스템이나
자율 swarm을 구현한 것으로 해석하면 안 된다. 새로운 message broker도 없다.

```text
사용자 ↔ CLI / Conversation
          ↓ 목표·지시 기록
      Attack Path Planner → 검증 질문·공격 경로 제안
          ↓
      Task Atomizer → 역할·대상·의존·완료 조건을 가진 작업 DAG
          ↓ Research/Evidence 경로 검토 + 코드 소유 검증
      Engine/Runner → 목적·예산·대상·경로를 바인딩한 위임
          ├─ Web 실행 역할 → 다음 행동 제안
          └─ Mobile 실행 역할 → 다음 행동 제안
                        ↓
      Catalog/인자·대상 검증 → Control → Gateway → MCP Adapter/서버
                        ↓
      원본 Evidence + 등록 Mapper → Attack Graph
                        ↓
      Research/Evidence 검토 → 계속 / 보류 / 작업·목표 완료 제안
                        ↓
      코드의 완료 게이트·기존 원장 → 재계획 또는 Report
```

## 2. 역할별 책임과 제한

역할 이름과 단계 연결은 현재 프로필의 구성이다. 일반 Engine의 고정된 7개 이름
허용 목록이 아니며 등록/설정으로 바꿀 수 있다. 다른 프로필은 구성이 다를 수 있다.

| 현재 역할 ID | 하는 일 | 대상 실행 | MD 시스템 지침 |
| --- | --- | --- | --- |
| `conversation-agent` | 사용자 대화에서 의도·질문을 해석하고 CLI의 입력 처리에 판단을 제공 | 불가. 명시 승인·중단 처리는 코드가 담당 | `conversation.md` |
| `attack-path-planner` | 목표·현재 증거를 바탕으로 검증 질문, 경로 및 다음 조율 결정을 제안 | 직접 실행 불가 | `attack-path-planner.md` |
| `task-atomizer` | 질문을 작은 작업으로 나누고 역할·알려진 대상·의존·완료 조건 연결 | 직접 실행 불가 | `task-atomizer.md` |
| `web-execution-agent` | 위임 목적에 맞춰 Web/API·Recon 도구와 실제 인자를 선택하고 후속 방법 제안 | 승인된 도구만 가능 | `web-execution.md` |
| `mobile-execution-agent` | 알려진 기기/앱/APK 대상의 Mobile·Analysis 작업 방법과 인자 제안 | 승인된 도구만 가능. 실제 기기 환경은 별도 필요 | `mobile-execution.md` |
| `research-evidence-agent` | 경로·해석·주장·작업/목표 완료·증거 재사용 조건·코드 검토 | 독립 대상 실행 역할 아님 | `research-evidence.md` |
| `exploit-code-agent` | 성공한 같은 Run의 근거로 fixture용 코드 산출물 작성 | 실제 대상 코드 실행 불가 | `exploit-code.md` |

지침 디렉터리는 [`config/agents/prompts/`](../../config/agents/prompts/)다.
공통 지침 `common.md`와 역할 지침을 결합하고 해당 단계의 출력 schema를 제공한다.
개발용 루트 `AGENTS.md`는 이 실행 프롬프트나 ROE의 대체물이 아니다.

Research/Evidence의 역할 통합은 연구·평가 책임을 묶은 것이다. 모든 검색·GitHub·VDB
MCP가 이 역할에 자동 설치되거나 독립 조사 워커로 연결됐다는 뜻은 아니다.
공개 자료 조회 도구를 사용하려면 실제 catalog·역할 노출·허용 대상·local-tool 경로를
별도로 갖춰야 한다. 경로 검토·원본 자료 접근·외부 검색은 서로 다른 기능이다.

현재 프로필에서는 Planner가 `attack_path_proposal` 및 조율용 `next_action`,
Atomizer가 `task_atomization`, Research/Evidence가 `path_review`,
`completion_review`, `task_completion_review`, `goal_completion_review`,
`analysis_verify`, `analysis_claim_verify`, `code_review`를 담당한다.
`code_generation`은 Exploit Code에 연결된다.
**조율 단계 `next_action`과 실행 워커가 자신의 목적을 위해 고르는 행동은 구분한다.**

## 3. 서브에이전트가 실제로 일하는 루프

1. App은 선택한 YAML과 MD를 검증하고 역할 registry, 단계별 지침 snapshot,
   계획 provider를 조립한다. 역할별 버전·hash·프로필 fingerprint를 저장한다.
2. Planner/Atomizer의 제안을 알려진 대상·사용자 참조·의존·작업 상한·역할·도구
   계약으로 검증한다. 존재하지 않는 계정이나 도구를 모델이 만든 것으로 채우지 않는다.
3. 위임은 목적, agent ID, path/step/version, MissionTask, 시간·action·판단·token
   예산을 바인딩한다. 단순히 “이 명령을 실행해라”라는 고정 행동열만 전달하지 않는다.
4. 워커는 역할별 현재 catalog, schema, 관련 그래프·증거, 같은 action의 Control
   기록, 기존 진행·미해결 상태를 받아 다음 행동을 선택한다.
5. 인자·대상·현재 경로를 검증하고 Control scope/ROE/위험/승인/예산을 적용한다.
   대기 큐 이후 실제 도구 시작 직전에도 admission을 재검사한다.
6. ToolResult 원본과 등록된 Mapper의 구조화된 관측을 저장한다. 외부 자료나
   도구 성공만으로 Finding이나 조사 목표 완료를 확정하지 않는다.
7. 결과·증거 평가에 따라 후속 행동, 보류, 경로 수정 또는 완료를 제안한다.
   최종 완료 게이트는 실제 근거·목표 조건·미처리 상태를 확인하는 코드다.

`DelegationState`에는 목적·경로·사용 예산·증거 ID·요약·미해결·보류 상태가 보존된다.
위임 상태, 실행 Work Ledger, Evidence Store, Attack Graph, Mission/Path는
서로 역할이 다르며 하나의 임시 프롬프트 문자열로 대체하지 않는다.

## 4. 병렬성·복구·예산

### P0 / P1 / P2

| 선택 파일 | 구현 특징 |
| --- | --- |
| `config/agents/specialists.yaml` | P0 직렬 전문 역할·Planner/Atomizer·MD snapshot |
| `config/agents/specialists-p1.yaml` | P1 durable 작업·경로 사건·독립 실행 워커 배치·복구 |
| `config/agents/specialists-p2.yaml` | P2 판단 격리·유한한 병렬 요청 풀·증거 유효성·코드 산출물 |

현재 P2 프로필은 version **6**, 실행 배치 **최대 2**, 판단 동시성 **최대 2**,
공통 판단 pool **64회**, 질문당 분해 작업 상한 **6**, 증거 만료 관련 총 대기
예약 **30초**를 선언한다. 이 숫자는 모든 실행의 권한이나 전체 모델 호출 허가가
아니다. 전역 실행 상한, 역할별 동시성, 위임 예산, Control과 실제 요청 정산이
함께 적용되므로 실제 허용량은 더 작을 수 있다.

같은 MCP backend·네트워크 대상·opaque 자원 참조를 공유하면 보수적으로 직렬화한다.
배치 2가 항상 대상 요청 2개의 동시 실행이나 성능 2배를 의미하지 않는다.

P2는 불변 문맥·격리된 판단 상태를 이용하고, 반영 전에 관련 대상·근거·목표 변경을
확인한다. 형제 판단이 끝나도 예산을 복제하거나 실제 요청 비용을 환급하지 않는다.
기존 Coordinator·원장·outbox·checkpoint 경로를 재사용해 사건과 결과를 조정한다.

정상 판단, 잘못된 응답 repair, 공급자 transport 실패를 구분한다. 같은 LLM client의
429 cooldown은 공유하지만 프로세스/계정 전체의 분산 limiter는 아니다. 명시된
쿼터 부족은 재시도하지 않으며 공급자 대기·요청 한도 소진은 `provider_unavailable`과
미해결 기록으로 남긴다. 무한 반복하거나 실패를 목표 성공으로 바꾸지 않는다.

## 5. 증거 역할과 코드 역할의 연결

- `evidence.assess`: 원본/주장·대상·세션·조건을 바인딩한 재사용 평가를 원본에
  덧붙인다. 원본을 삭제·수정하지 않는다. `unbound`는 자동 실패/성공 판정이 아니다.
- 명시적으로 알려진 만료 조건은 관련 held 위임만 유한한 대기 안에서 재평가한다.
  모든 증거를 무한히 감시하거나 종료된 Run을 자동 재시작하지 않는다.
- `code.prepare`: Exploit Code가 fixture용 산출물을 작성하고 별도 Research/Evidence
  역할이 검토한다. 원본 근거·정책·입력·산출물 hash를 연결한다.
- `code.test`: 정확히 검토한 artifact를 선택된 캐시 이미지의 network-none Docker에서
  시험한다. 실제 출력과 기대값 비교는 호스트가 수행하며 생성 코드의 자기 합격
  숫자를 믿지 않는다. 범용 호스트 shell이나 실제 대상 네트워크 실행은 열지 않는다.

코드 생성·fixture 합격은 실제 취약점 재현이나 Finding이 아니다. 현재는 승인된
실제 대상 코드 실행 규격이 미확정이므로 비활성이다. Docker 설치·캐시 이미지가
없으면 fixture 시험도 가능하다고 할 수 없다.

## 6. 관련 코드를 읽는 순서

| 파일/영역 | 확인할 내용 |
| --- | --- |
| `config/agents/specialists-p2.yaml`, `prompts/*.md` | 역할·단계·한도·프롬프트 선언 |
| `app/agent_profile.py`, `app/bootstrap.py`, `app/runtime_components.py` | 검증·version/hash·등록·구현 주입·resume binding |
| `orchestration/prompts.py`, `specialist_planning.py` | 요청별 역할 지침·Planner→Atomizer·경로 검토 |
| `orchestration/delegation.py`, `delegated_runtime.py` | 조사 목적·예산·워커 상태·후속 행동 |
| `orchestration/parallel_reasoning.py`, `resource_conflicts.py`, `dispatch.py` | 판단 격리·pool 정산·실행 충돌·배치 |
| `orchestration/path_events.py`, `outbox.py`, `coordination.py`, `work/` | 경로 사건·원자 반영·lease·복구·중복 처리 |
| `orchestration/graph.py`, `nodes/` | 승인·실행·증거·재계획 루프 |
| `control/`, `capability/registry.py`, `catalog.py`, `gateway.py` | 승인과 실제 도구 선택·MCP 실행의 경계 |
| `orchestration/evidence_validity.py`, `evidence_expiry.py`, `code_artifacts.py` | P2 평가·유한 대기·fixture 산출물 |
| `orchestration/completion.py`, `reporting.py` | 완료 근거 검사·미해결 보고 |

Engine과 병렬 판단 모듈 사이의 내부 결합·큰 함수는 아직 남아 있다. 이번 검수는
이 구조 전체를 무리하게 다시 쓰지 않고 확인된 네 문제만 국소 수정했다.

## 7. 이번 검수에서 바뀐 사항

1. HTTP 인증 관측의 `web.http_request` 문자열 고정을 제거했다. 명시적으로 연결한
   Mapper가 검증한 관측을 사용하므로 physical ToolId에도 증거가 보존된다.
2. 인증 비교에서 기준/현재 증거의 동일 Run과 source/action/invocation·요청 대응을
   확인한다. 다른 Run 또는 불일치 자료를 성공 근거로 사용하지 않는다.
3. audit/blocked Evidence 마스킹을 기존 Core redaction에 모아 Control↔Evidence
   역방향 의존을 제거했다. 기존 import는 호환 re-export로 보존했다.
4. 플래너의 사용자·세션 참조 조회를 기존 knowledge validation으로 옮겼다.
   projection 조회가 실행 허가를 주지는 않으며 fresh 실행 검사는 유지한다.

자세한 근거·남은 유지보수 부담은 [전체 검수 보고서](modularity-review-20261008.md)를
참고한다. 서브에이전트 성능을 실측으로 보장하거나 특정 랩의 정답을 코드에 넣은
변경이 아니다.

## 8. 새 개발자가 실행할 때

저장소 루트에서 자신의 Python 환경을 만들고 승인 engagement, LLM 공급자/모델과
MCP 실행 설정을 준비한다. 개인 `.env`, `data/`, Run 보고서, 기기 정보·보안키는
브랜치에 배포하지 않는다. 다른 개발자의 절대 경로가 든 실행 환경 파일을 재사용하지 않는다.

```sh
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev,agent]"

# LLM·승인 engagement·MCP 연결 설정을 준비한 다음 명시적으로 선택한다.
export REDTEAM_SPECIALIST_PROFILE="$PWD/config/agents/specialists-p2.yaml"
python -m redteam.app.cli
```

P2 선택만으로 MCP 서버·기기·시험 권한이 생기지 않는다. 기존 기본 모드는
프로필 환경변수를 unset하면 선택되며 최신 프로필을 임의 강제하지 않는다.
MD/프로필을 바꾸면 새 Run으로 시험한다. 기존 Run resume은 원래 fingerprint를
확인하므로 변경 지침을 같은 Run에 몰래 적용하지 않는다.

## 9. 가능한 것과 아직 미확인인 것

- 구현·합성 검증: 전문 역할 선택, 목적 위임, 도구 선택·Control·MCP·원본 증거,
  병렬 판단/배치·취소/중복/복구, 작업·목표 완료 검토, 증거 평가, fixture-only 코드.
- P3: 승인 기기·앱·정책을 받을 설정 생성과 Mobile 합성 연결까지 준비했다.
  실제 기기/앱 평가 완료는 아니다.
- 실제 모델 조사 품질 게이트는 미통과다. 이전 비교의 목표 달성·실제 금융권
  취약점 탐지·429 해소를 주장하지 않는다. 자세한 이력은
  [P2 검증](p2-completion-20261008.md), [P3 준비](prd-v2-p3-preparation-20261008.md),
  [공급자 처리](provider-backoff-and-profile-20261008.md)에 있다.
- 코드 검수 시 전체 로컬 회귀는 **3,145 passed / 61 skipped**,
  Ruff 및 Mypy **614개 파일** 통과했다. 실제 공급자·기기·외부 MCP·별도 Docker
  opt-in은 이 전체 회귀에서 제외했으며 skip을 실제 검증 통과로 세지 않았다.
- 브랜치 공유 직전 관련 시험 **183 passed**와 Ruff·Mypy를 재확인했다.
  세 프로필 로드·새 안내 문서 링크·staged 개인 데이터 경로 및 대표 토큰/키 패턴도
  검사했다. 패턴 검사만으로 모든 형태의 비밀정보 부재를 보증하는 것은 아니다.

이 안내는 branch 공유용 현재 요약이다. 하위 날짜별 문서에 있는 “로컬만 변경,
push 안 함”과 과거 테스트 수치는 그 문서 작성 시점의 기록이다.
