# 전체 소스 구조 검수 및 국소 수정 — 2026-10-08

## 1. 결론과 기준

현재 코드는 **기본 계층·등록·실행 승인 경계는 모듈화되어 있지만, 내부 결합과 유지보수 부담까지 해소된 상태는 아니다.** 특정 도구 이름에 묶인 인증 관측, 인증 비교의 증거 귀속 누락, Evidence→Control 역방향 의존, 플래너의 Engine helper 의존을 확인하고 수정했다.

검수 대상은 이 문서와 같은 로컬 저장소이며 브랜치는 `dev-jm`, 시작 HEAD는 `ec1b8d9`다. 기존 P0~P3 및 공급자 재시도 관련 미커밋 변경을 포함한 현재 파일을 기준으로 검수했다. 원격 최신 dev와 같다는 뜻은 아니다. 이번 작업은 pull/fetch/commit/push/PR 없이 진행했다.

`AGENTS.md`와 Core/App/Orchestration/Capability/Control의 영역별 규칙을 적용했다. 단순 파일 크기나 문자열 개수 대신 **책임의 소유자, public contract/port, 등록 기반 확장, 승인·증거·재개 흐름**을 기준으로 판단했다.

## 2. 검수 범위와 한계

- 생산 Python 소스 337개 전체에 AST 파싱, 정적 import 방향, 사용자 절대 경로·랩 주소 문자열, 동일 함수 본문, bare except 검사를 수행했다. 검수 시작 시 약 83,815줄이었다.
- 조립, 역할·도구 등록, 계획, 실행 직전 승인, MCP gateway, 결과 Mapper, 인증 비교, 증거 저장, 그래프 반영, checkpoint/resume, 보고 흐름의 핵심 코드를 직접 확인했다.
- 관련 설정 schema·P2 프로필·기존 검증 문서와 테스트를 대조했다.
- **83,815줄 모두의 의미를 줄마다 수동 검증했다는 뜻은 아니다.** 정적 검사는 동적 import·외부 서버 내부 구현·실제 LLM 판단의 정확성을 증명하지 않는다.
- 실제 LLM 요청, 금융 사이트 점검, ZAP/Burp 실환경 재연결, 실제 기기·Docker 코드 runner 실행은 하지 않았다. 실행을 검증한 부분과 준비·정적 검사만 한 부분을 구분한다.

## 3. 주요 책임과 실제 연결

| 영역 | 구현 접점 | 소유하는 책임 |
| --- | --- | --- |
| App/CLI | `app/bootstrap.py`, `app/runtime_components.py`, `app/agent_profile.py`, `app/cli/` | 설정 snapshot, 구현 주입, 버전·프로필 선택, 입력·승인·렌더링 |
| Core | `core/contracts/`, `core/ports/`, `core/tool_binding.py` | 팀 간 자료형·port·실행 binding·기계적 변환 |
| Agent | `orchestration/engine.py`, `specialist_planning.py`, `delegated_runtime.py`, `parallel_reasoning.py` | 조사 경로·원자화·위임·판단 예산·병렬 판단 |
| 실행 루프 | `orchestration/graph.py`, `nodes/`, `runner.py` | Observe→Plan→Policy→Execute→Verify→Update→Replan 및 재개 |
| Control | `control/service.py`, `scope.py`, `roe.py`, `approval.py` | 범위·위험·승인·요청/시간 예산, 실행 직전 재검사 |
| Capability | `capability/registry.py`, `catalog.py`, `gateway.py`, `adapters/mcp/` | discovery 보관, 작업 의미 binding, schema 검증, adapter 라우팅 |
| 결과·증거 | `orchestration/observations/`, `nodes/verify.py`, `recovery.py`, `evidence/` | 등록된 Mapper의 관측, 원본 결과 보존, 재처리·귀속 |
| Graph·종료 | `attack_graph/`, `orchestration/completion.py`, `reporting.py` | 관측/가설/검증 결과 구분, 완료 판정·보고 투영 |

실행 경로는 App 조립 → Core port로 주입된 catalog/gateway → Agent 제안·인자 검증 → Control → Gateway/Adapter → 원본 Evidence와 Mapper → Graph/후속 판단 → Report다. Agent가 Capability adapter 구현체를 직접 import하는 정적 경로는 발견하지 못했다. 발견·등록·health만으로 실제 선택·승인이 완료되는 구조도 아니다.

## 4. 발견 사항과 수정

### R1 — 인증 관측이 옛 ToolId에 고정됨: 수정

근거: `orchestration/authentication.py`의 `http_observation`은 `web.http_request`만 허용했다. Registry가 `web.<server_id>.<remote_tool_name>`을 제공하고 Mapper까지 연결해도, 내부 helper가 결과를 버렸다. `assess_authentication`도 기준 Evidence의 `source_id`를 옛 이름과 비교했다.

재현: 명시적으로 HTTP Mapper에 등록한 `web.http-a.http_request`와 `web.http-b.fetch_response`의 결과에서 `http_response` 관측이 누락되는 테스트 실패를 확인했다.

수정:

- HTTP helper에서 ToolId 문자열에 의한 호환성 판단을 제거했다.
- 공통 VERIFY는 임의 도구의 출력에서 HTTP처럼 보이는 값을 직접 추출하지 않는다. **기존 등록 Mapper가 전체 응답 계약을 검증한 뒤 반환한 관측**만 Evidence에 보존한다.
- 관측이 여러 개여서 대응이 불명확하면 임의로 하나를 고르지 않는다.
- 인증 비교는 이 보존된 관측과 실제 결과를 대조한다. 도구 prefix나 remote 이름 추측으로 Mapper를 자동 연결하지 않는다.

미등록 Mapper와 응답 계약 불일치에 대해서는 원본 ToolResult를 보존하지만 인증 관측으로 받아들이지 않는다. 두 부분 legacy 이름도 기존 명시적 등록 경로에서 계속 사용 가능하다.

### R2 — 인증 비교에서 Run·현재 결과 귀속 확인 부족: 수정

근거: 기존 기준 증거 검사에는 engagement 비교는 있었지만 Run 비교가 없었다. 현재 Evidence의 source/action/invocation 귀속과 현재 보존 관측의 path/status도 비교하지 않았다. 다른 Run의 기준 증거 및 현재 source/path 불일치가 성공 비교에 쓰일 수 있음을 합성 재현으로 확인했다.

수정:

- 기준·현재 Evidence·ActionProposal의 동일 Run/engagement를 확인한다.
- TOOL source type과 현재 결과의 source/action/invocation 대응을 확인한다.
- 현재 관측의 method/path/target_ref/session_ref/status가 요청과 실제 결과에 대응해야 한다.
- 실패·관측 누락·모호한 자료는 성공 근거로 쓰지 않는다.

`session_access_observed`는 세션 요청에 응답이 있었다는 관측이다. 이것을 취약점이나 실제 로그인·권한 우회 성공과 동일시하지 않는다. 기존 승인·Finding 승격 조건은 완화하지 않았다.

### R3 — Control↔Evidence 순환 의존: 수정

근거: Control은 EvidenceService를 사용하고, EvidenceService는 마스킹을 위해 Control의 audit 구현을 지연 import했다. 지연 import는 import 시점을 미룰 뿐 소유권 순환을 제거하지 않는다.

수정: audit/blocked Evidence에서 함께 쓰는 순수 `redact_arguments`를 기존 `core/redaction.py`로 옮겼다. 두 소비자가 공통 함수를 사용하며 `control.audit.redact_arguments`는 호환 re-export로 남겼다. 넓은 audit용 key masking과 opaque reference를 보존하는 모델 projection 정책은 서로 다른 의미라 강제로 합치지 않았다. 기존 마스킹 정규식 의미와 입력 불변성은 유지했다.

수정 후 영역별 정적 absolute import 그래프는 비순환으로 확인됐다. 이것은 동적 import 또는 세부 파일 사이의 모든 순환 의존이 사라졌다는 주장은 아니다.

### R4 — 플래너가 Engine을 사용자·세션 조회 라이브러리로 사용: 수정

근거: `specialist_planning.validate_strategy`가 사용자 참조 조회만을 위해 `orchestration.engine.principal_bindings`를 import했다. Engine 자체가 specialist planning을 사용하므로 helper 사용이 불필요한 역결합을 만들었다.

수정: 기존 `knowledge_validation.py`가 이 조회를 소유하도록 함수 본문을 이동했다. Engine은 호환 re-export, 플래너는 검증 모듈 직접 import로 변경했다. 조회 규칙은 바꾸지 않았다. 이 capped projection이 실제 실행 허가를 주는 것은 아니며 fresh InternalAssessment 기반 실행 검사는 유지했다.

## 5. 변경하지 않은 부분과 남은 부담

### 당장 결함으로 단정하지 않은 상수

- capability/operation·단계·schema version·프로토콜 식별자는 공유 계약 또는 제공 모듈 선언이다.
- `LLMConfig.default_model`의 모델명은 설정 기본값이며 주입/환경 설정으로 변경할 수 있다. CLI 입력 예시와 LLM adapter의 공급자 호환성 판정도 실제 모델을 중앙 Agent에 고정하는 분기와 구분했다.
- 프로필의 판단 동시성 최대 2, 질문당 작업 상한 6 등은 설정 schema에 표현된 안전 제한이다. P2 문서에도 판단 동시성 최대 2가 명시돼 있어 임의 제거하지 않았다.
- adapter/Mapper에서 특정 제품의 응답을 해석하는 코드는 해당 제공 모듈의 책임이다. 이를 중앙 Agent/Control로 옮기지 않았다.
- execution substrate의 허용 executable/옵션과 네트워크 차단 제한은 의도적인 보안 경계다. 자동 확장하지 않았다.
- 동일 함수 본문 검사에서 나온 recovery/completion의 alias grounding wrapper는 기존 한 검증 함수로 위임한다. 같은 정책의 독립 구현이 아니라 forwarding이므로 무의미하게 재구성하지 않았다.

생산 `src/redteam`에서 `/Users/`, `/home/`, 기존 개발자 이름, `mock-bank`, `/api/accounts/alice`, `127.0.0.1:8088`, `p0-live`를 검색했으나 고정된 실행 경로·랩 정답 문자열은 찾지 못했다. 이 제한된 검색만으로 모든 형태의 하드코딩이 없다고 보증하지 않는다. 로컬 ignored 실행 설정 및 fixture 예시의 값은 일반 실행 코드와 구분했다.

### 후속 검수 항목 — 이번에는 대규모 분해하지 않음

1. `engine.py`는 검수 시작 시 3,337줄이며 계획·판단·위임 조율 책임이 집중돼 있다. `parallel_reasoning.py`가 `_ask`, `_event_runtime`, `_ensure_reasoning_allowed` 등 Engine 내부 상태/메서드를 사용한다. 같은 Orchestration 구현 영역이지만 변경 영향이 넓다. 향후 판단 snapshot·budget/event accounting 경계를 기존 public entry point 안에서 먼저 명확히 하고, 취소·실패·병렬 정산 테스트를 유지하면서 한 책임씩 분리해야 한다.
2. `completion.py` 2,058줄, `knowledge.py` 1,852줄, `analysis.py` 1,639줄 등도 크다. 크기만으로 오류라 할 수는 없다. claim 검증·projection·collector의 책임 변경이 실제로 겹칠 때 분리해야 한다. 같은 이름의 capped context와 fresh 실행 자료를 혼동해 합치지 않는다.
3. `capability/gateway.py`는 scoped context 주입을 `ScopedAnalysisMCPAdapter` 상속 여부로 판정한다. 현 VDB/Analysis 구현은 이 경로를 사용하므로 즉시 실패로 확인되지는 않았다. 다른 종류의 scoped adapter가 필요해질 때 기존 adapter contract에 context-binding seam을 정의하는 것이 적절하다. 이번 검수 때문에 범용 plugin hook을 새로 만들지는 않았다.

이는 보류된 실제 위험/유지보수 항목이다. “전체 모듈화 완료”나 “모든 실행 실패 해결”을 선언하지 않는다.

## 6. 검증

새 `tests/unit/test_modularity_review.py`는 다음을 검사한다.

- legacy/physical/다른 remote 이름의 명시적 HTTP Mapper binding.
- production VERIFY에서 정상 관측 보존 및 미등록/잘못된 shape 거부.
- 동일 Run의 인증 비교와 다른 Run·source·path·status 자료 거부.
- Core·Agent·Capability·Control·Evidence의 정적 import 경계.
- 순환 의존 제거, 기존 import 호환성, 입력을 변경하지 않는 마스킹.

최종 검증 결과:

- 새 검수 회귀 테스트: **19 passed**. legacy ToolId에서도 미등록 Mapper·잘못된 응답 거부를 확인했다.
- 관련 node/specialist/extension/checkpoint 회귀 묶음: **116 passed**.
- 인증/Mapper/audit 관련 초기 회귀 묶음: **68 passed**.
- Ruff `check src tests`: 통과.
- Mypy `src tests`: **614 source files, 오류 없음**. 프로젝트 설정상 untyped 함수 본문의 일부는 검사하지 않으므로 모든 동적 경로가 타입 안전하다는 의미는 아니다.
- `git diff --check`: 통과.
- 최종 전체 로컬 회귀: **3,145 passed, 61 skipped, 154.58초**. 실제 공급자·기기·Docker/외부 MCP 검증을 비활성화하고 localhost 합성 서버 사용을 허용한 조건이다. skip은 통과나 실제 서버 검증 완료로 계산하지 않는다.

전체 회귀 재현 명령은 저장소 루트에서 다음과 같다. 기존 프로필과 실환경 opt-in을 이 테스트 프로세스에서만 제외하며 운영 설정 파일은 변경하지 않는다.

```sh
env -u REDTEAM_SPECIALIST_PROFILE \
  -u REDTEAM_TEST_REAL_ADB_SERIAL -u REDTEAM_TEST_MOBILE_E2E_DEVICE_SERIAL \
  -u REDTEAM_TEST_APKTOOL_E2E_APK -u REDTEAM_TEST_MOBSF_CONFIG \
  -u REDTEAM_VDB_UPSTREAM_SOURCE \
  LITELLM_LOCAL_MODEL_COST_MAP=True \
  REDTEAM_MCP_SERVERS='' REDTEAM_EXTERNAL_MCP_SERVERS='' \
  REDTEAM_TEST_FIXTURE_DOCKER=0 REDTEAM_TEST_EXECUTION_DOCKER=0 \
  REDTEAM_TEST_EXECUTION_LLM=0 REDTEAM_TAVILY_LIVE=0 REDTEAM_TAVILY_MCP_LIVE=0 \
  .venv/bin/pytest -q --maxfail=1
```

## 7. 실제 사용 시 알아둘 것

이번 수정은 새 MCP를 설치하거나 실행 허용 범위를 넓히지 않는다. 설정·정책·서버가 없는 도구는 여전히 실행할 수 없다. 기존 CLI 프로세스는 종료하고 다시 실행해야 수정 코드가 반영된다. 기존 Evidence 원본을 재작성하거나 기존 Run을 자동 성공 처리하지 않는다.

GitHub에 업로드하지 않았으며 기존 로컬 작업을 보존했다.
