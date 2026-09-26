# WHS와 로컬 Claude Code의 에이전트 구현 비교

작업 ID: `SOURCE-COMPARISON-20260925` · 분석일: 2026-09-25 · 상태: 정적 소스 분석 완료

## 1. 판정

**현재 WHS는 실행 가능한 보안 작업 오케스트레이터이며, Claude Code 소스에서 확인되는 범용 추론·도구 사용 에이전트의 기능에는 상당한 격차가 있다.** 가장 큰 차이는 모델이 다음 행동과 도구 인자를 만들고 결과를 읽어 다시 추론하는 주체인가, 코드가 만든 후보의 순서를 보조하는가다.

WHS에는 실제 실행 루프, 도구 연결, 입력 검증, 정책 검사, 병렬 행동 실행, 증거 저장, 판정, 보고가 있다. 따라서 “껍데기뿐”이라는 평가는 맞지 않는다. 반면 이 기반만으로 자유로운 조사, 새로운 분석 절차 작성, 임의 결과의 해석, 독립 하위 에이전트 협업, 중단된 장기 작업의 완전 복구가 구현됐다고 볼 수도 없다.

현재 구조를 가장 정확히 표현하면 **“그래프 상태에 반응하는 규칙 기반 보안 워크플로에 제한된 LLM 후보 선택을 붙인 시스템”**이다. 정적인 순서만 실행하는 스크립트보다는 발전했지만, 범용 에이전트의 행동 생성과 조사 범위에는 못 미친다. 이 차이는 모델을 더 강한 것으로 바꾸거나 프롬프트만 수정해 해결되지 않는다.

도메인이 다르므로 코드 편집기·노트북·IDE 같은 기능을 모두 필수 결손으로 계산하지 않았다. 대신 보안 분석에서도 필요한 계획 수립, 자료 읽기, 도구 조합, 장기 실행, 실패 복구, 상태 재개, 사용자 지시 반영을 중심으로 비교했다. 범위·ROE·증거 기반 취약점 판정은 WHS의 도메인 자산으로 별도 평가했다.

## 2. 기준과 분석 범위

| 항목 | 기준 |
|---|---|
| WHS 위치 | `C:/Users/qkrgu/Documents/GitHub/WHS-Red-Teaming-Agent` |
| WHS 분석 기준 commit | `be3a0de4d87e2b2a282ef0136f5143dda0e0fd1a` |
| Claude 위치 | `C:/Users/qkrgu/Documents/GitHub/claude-code` |
| Claude 분석 기준 commit | `bf9c36c3fd90f218f1ee48b77c6c5ba217d02603` |
| 시작 상태 | 두 저장소 모두 working tree clean |
| 분석 방식 | 주 에이전트의 직접 코드 추적. Luna 6 xhigh 서브에이전트 2개는 읽기 전용 파일·심볼 위치 탐색만 수행 |
| 수행하지 않은 검증 | 외부 대상 실행, 실제 LLM 호출, MCP 실서버 실행, Claude 빌드, 성능·취약점 발견률 측정 |

이 보고서는 **로컬 소스에 구현된 경로 비교**다. 현재 공개 Claude 제품의 전체 기능이나 모든 배포 환경에서의 활성화를 주장하지 않는다. 비교 폴더의 README와 CLAUDE.md는 해당 트리를 sourcemap 기반 아카이브 및 여러 버전의 선택적 복구 내용이 합쳐진 소스로 설명하며, `src/`의 독립 빌드 구성이 없다고 명시한다. 사용자가 설명한 제공 경위와 별개로, 분석자가 확인할 수 있는 코드 기준은 이 로컬 checkout이다. 복구 소스의 완전성이나 원본과의 일치 여부를 새로 인증한 것은 아니다. [C00]

WHS `src` 파일 192개와 Claude `src` 파일 2,184개를 목록화했다. **목록화한 모든 파일의 모든 줄을 정독했다는 뜻은 아니다.** 상세 대조 대상은 아래 22개 기능의 실제 실행 경로와 연결 모듈이다. UI 세부 컴포넌트, 모든 운영체제별 분기, 복구 도구 전부를 행 단위로 감사한 것은 아니며, 이를 완전 전수감사로 표기하지 않는다. 파일 수와 코드 길이는 성숙도 점수로 사용하지 않았다.

## 3. 실행 경로의 핵심 차이

### WHS의 기본 경로

```text
CLI / 설정 대화 → Engagement 설정 → bootstrap
→ OBSERVE: 그래프 관측과 역할 선택
→ PLAN: provider가 후보 생성 → compiler 검증·도구 결합 → 점수화
        → HybridPlanner가 확정 순위 또는 제한된 LLM 재정렬
→ POLICY → EXECUTE → VERIFY → UPDATE → REPLAN
                         ↓         ↓
                       증거      그래프·작업 대장
→ 종료 상태와 보고서

후보가 여러 개일 때: PLAN → Send(worker) → aggregate → REPLAN
worker 한 개: POLICY → EXECUTE → VERIFY → UPDATE
```

`graph.py`의 노드 이름만 보고 병렬 경로가 끊겨 있다고 판단하면 안 된다. `router.route_after_plan()`의 `Send("worker", ...)`가 실제 fan-out을 연결한다. [W01] [W02] [W03] [W04]

### Claude 로컬 소스의 경로

```text
사용자 입력 / 세션 복구 → QueryEngine / REPL → query 반복
→ 대화·시스템 문맥·도구 목록으로 모델 호출
→ 모델이 tool_use와 인자 생성
→ 스키마·권한·hook → 도구 실행 / 병렬 처리
→ tool_result와 새 메시지 반영 → 모델이 후속 추론
→ 완료, 중단, 재시도 또는 컨텍스트 축약

AgentTool → runAgent → 독립 문맥으로 같은 query 반복
                       + transcript / background task 관리
```

차이는 반복문의 유무가 아니다. **WHS는 코드가 아는 행동을 순환하고, Claude 경로는 허용된 도구 안에서 모델이 행동의 조합과 인자를 구성한다.** 물론 Claude도 권한·도구 스키마·턴 제한에 묶여 있으며 무제한 실행 구조는 아니다. [C01] [C02] [C03] [C04]

## 4. 기능별 격차 요약

표의 ‘큰 격차’는 해당 기능을 실사용하려면 구조 또는 통합 구현이 필요하다는 뜻이다. ‘부분 격차’는 동작 경로가 있으나 기능 범위·복구·사용자 노출이 제한된 경우다. 성공률이나 코드 완성률의 수치가 아니다.

| # | 기능 | WHS 현재 상태 | 비교 판정 |
|---|---|---|---|
| 1 | 시작·설정·실행 진입 | CLI, 대화형 설정, 실모델/MCP 연결 있음 | 부분 격차: 개발용 기본값 구분 필요 |
| 2 | 다음 행동 생성 | provider와 playbook이 생성, 모델은 후보 선택 | 큰 격차 |
| 3 | 도구 호출을 이어가는 추론 | 규칙 기반 그래프 피드백 루프 | 큰 격차: 일반적인 모델 도구 루프 없음 |
| 4 | 파일·코드·출력 탐색 | 전용 도구와 특정 결과 계약 중심 | 큰 격차: 조사용 범용 작업 수단 제한 |
| 5 | 결과 해석·새 정보 편입 | 도구별 mapper와 관측 구조 있음 | 부분 격차: 지원 형식 밖 해석 제한 |
| 6 | 모델 출력 구조 보장 | JSON 파싱과 선택 ID 확인 | 큰 격차: 일반 경로 schema 계약 미충족 |
| 7 | API 오류·재시도 | planner의 결정론적 fallback | 큰 격차: 응용 수준 재시도 제어 미연결 |
| 8 | 도구 재시도·대체 행동 | 오류 정규화, 종료·중복 억제 | 큰 격차: 분류별 회복 정책 없음 |
| 9 | 긴 작업·백그라운드 | lease heartbeat, 동기 결과 대기 | 큰 격차: 공통 job 수명 관리 없음 |
| 10 | 병렬 작업 | Send worker, 충돌 키, 서버별 semaphore | 부분 격차: 실행 가능, 기본 CLI는 순차 |
| 11 | 독립 하위 에이전트 | 역할 provider 및 단일 행동 worker | 큰 격차: 독립 추론 세션과 다름 |
| 12 | 컨텍스트 크기 제어 | 역할·대상별 그래프 투영, 개수 제한 | 부분 격차: 모델 문맥 압력 관리 부족 |
| 13 | 큰 출력의 보존·재열람 | 초과 출력 제거, 별도 artifact 기반 존재 | 큰 격차: 일반 실행 경로의 회수 기능 부족 |
| 14 | 세션 저장·프로세스 재개 | 체크포인트, 증거·승인 저장 | 큰 격차: 통합 복구와 CLI 진입 없음 |
| 15 | 실행 중 지시·질문·중단 | 설정 대화, 승인 대기, runner 중단 | 부분 격차: 실행 중 대화형 조종 부족 |
| 16 | MCP 연결·확장 | HTTP/stdio, 발견·manifest·스키마 | 부분 격차: 연결 복구·풍부한 프로토콜 통합 |
| 17 | skill·동적 확장 | Python registry/provider 확장 | 큰 격차: 사용자 설치형 실행 지침 체계 없음 |
| 18 | 비용·관측성 | 행동 수, 이벤트, 증거, 작업 상태 | 부분 격차: 모델 비용·토큰 계량 부족 |
| 19 | 범위·권한·ROE | 실제 정책 게이트와 승인 | 도메인 강점; 범용 sandbox와는 별개 |
| 20 | 취약점 검증·증거 | 판정/승격 분리, 증거·가설 연결 | 도메인 강점; 검증 종류는 제한 |
| 21 | 종료·보고·충족 판단 | 수렴 검사와 구조화 보고 | 부분 격차: 후보 소진과 목표 달성 구분 필요 |
| 22 | 설정·선언과 실행의 일치 | 일부 선언과 실제 연결 불일치 | 우선 정리할 구현 공백 |

## 5. 기능별 상세 대조

### 5.1 시작·설정·실제 실행의 구분

WHS는 `pyproject.toml`의 CLI 진입점, `cli` dispatcher, 대화형 engagement 수집, preflight와 `build_application()`을 갖췄다. 설정 수집이 끝나면 대화형 명령도 일반 `run`의 build/execute 경로를 사용한다. 이것은 서로 다른 실행기를 중복 구현한 구조가 아니다. [W05] [W06] [W07]

다만 `LLMConfig.provider` 기본값은 `fake`이고, `build_llm_client()`는 문자열이 정확히 `litellm`일 때만 실모델 클라이언트를 만든다. 다른 문자열도 Fake로 내려간다. `capability.mcp_servers`가 비면 Example adapter를 사용한다. CLI가 이에 대해 경고하는 점은 이미 구현돼 있다. [W08] [W09]

**실사용 영향:** 프로세스가 성공적으로 시작됐다는 사실과 실제 모델·실제 보안 도구가 붙었다는 사실은 다르다. 특히 모델 provider 오타가 잘못된 설정으로 거부되지 않고 fake 경로를 고를 수 있다. “기본 설치만 하면 실전 에이전트로 작동한다”는 판정은 불가하다. Claude는 모델 호출·도구 루프가 핵심 진입 경로이며 다양한 실행 옵션이 있지만, 이 로컬 소스 자체의 설치·실행 성공은 이번에 검증하지 않았다. [C01] [C05]

### 5.2 계획은 후보 선택에 제한된다

WHS의 `build_planner("llm", ...)`는 `HybridPlanner`를 반환한다. 후보가 하나이거나 점수 차가 충분하면 모델을 호출하지 않는다. 모호할 때만 기본 상위 3개를 재정렬하며 반환값은 제시된 `option_id`여야 한다. `PlanStep.arguments`를 모델이 생성·수정하는 경로는 이 플래너에 없다. 기본 prompt도 제공하지 않은 단계·도구·대상을 만들지 말라고 명시한다. [W10] [W11]

`ReconCandidateProvider`와 `WebCandidateProvider`는 그래프 관측을 근거로 후속 후보를 생성하므로 WHS 전체를 고정 playbook 재생기로만 부르는 것도 틀리다. 다만 이 새 후보는 Python에 구현된 분기에서 나오며, LLM이 새로운 조사 절차를 구성하는 것은 아니다. [W12] [W13]

Claude `query()`는 모델 응답의 `tool_use` 블록을 받아 실제 도구 입력으로 연결하고 결과를 다음 모델 호출로 보낸다. **WHS의 가장 큰 격차는 후보 수가 적다는 문제를 넘어, 모델에게 행동을 생성할 계약 자체를 주지 않았다는 점**이다. 도구 추가만으로 조사 능력이 자동 확장되지 않는다. [C01] [C03]

### 5.3 반복 추론과 오류를 통한 자기 수정

WHS에는 OBSERVE→PLAN→실행→그래프 갱신→REPLAN이 실제 존재한다. 새로운 관측이 생기면 역할을 다시 활성화하고, 진전이 없거나 예산이 소진되면 멈춘다. 이 기능은 제대로 인정해야 한다. [W01] [W14]

그러나 모델이 방금 실행한 호출과 상세 오류를 대화 기록으로 받아 “이 인자가 틀렸으니 수정한다”, “이 파일을 먼저 읽어야 한다”라고 다음 행동을 재작성하는 공통 경로는 확인되지 않는다. 모델 입력은 새로 렌더링한 그래프 요약·후보·일부 증거다. WHS의 replan은 주로 역할과 후보를 다시 계산하는 재계획이다. Claude는 메시지와 tool_result를 루프 상태로 유지하고 재호출한다. [W10] [W03] [C01]

**영향:** 처음 보는 오류나 환경 차이를 만났을 때, 모델이 원인을 이해할 능력이 있더라도 현재 provider가 그 대응 후보를 생성하지 않으면 행동으로 이어지지 않는다.

### 5.4 조사 작업의 기본 수단

Claude 도구 풀에는 Read, Write, Edit, Glob, Grep, Bash, Agent, TaskOutput/Stop 등의 구체적 구현이 연결돼 있다. 단순한 도구 이름 목록에 그치지 않고 공통 dispatcher가 입력 검증·권한·호출·결과 반환을 수행한다. [C06] [C03] [C07]

WHS는 HTTP, 정찰, 프록시, Nmap, Nuclei, APK 분석 등 전용 서버를 가진다. 그러나 기본 에이전트가 임의 소스 트리를 검색하고, 필요한 일부를 읽고, 로컬 분석 코드를 만들고, 실행 결과를 해석해 수정하는 통합된 작업 공간 도구 흐름은 기본 registry/provider에서 확인되지 않는다. 분석 서버의 APK 목록·digest·문자열 검색은 실제 기능이지만 범용 코드 이해·편집·실행 워크스페이스와 범위가 다르다. [W12] [W15] [W16]

**필요한 방향:** 코딩 에이전트 기능 전부를 복제할 필요는 없다. 보안 조사에 필요한 제한된 파일 읽기·검색·자료 재열람·분석 스크립트 실행을 모델 행동 계약에 연결하는 것이 핵심이다. 임의 셸 실행을 추가하는 것만으로 이 문제가 해결되지는 않는다.

### 5.5 결과를 ‘이해할 수 있는 정보’로 바꾸는 경로

WHS `ResultMapperRegistry`는 정확한 tool ID별 mapper를 사용한다. 성공한 결과에서 새 서비스·엔드포인트·관측을 그래프에 넣고, 실패나 형식 불일치를 이벤트로 남긴다. 지원되지 않은 도구 결과는 일반적으로 새 graph mutation을 만들지 않는다. [W17] [W18]

이 방식은 관측되지 않은 사실을 그래프에 넣지 않는 장점이 있다. 반면 **도구가 성공했다고 해서 그 도구가 주는 지식이 자동으로 후속 계획에 반영되는 것은 아니다.** 새로운 도구에는 적절한 후보 생성, canonical operation 결합, 결과 해석과 검증 경로가 함께 필요하다.

HTTP provider는 제한된 응답 관측을 사용한다. 해당 기본 `http_request` 계약은 body digest·크기·status 등의 요약을 제공하고, raw header/body를 그대로 모델에게 주는 경로가 아니다. 다른 전용 도구가 추가 추출을 제공할 수 있으므로 “모든 HTTP 도구가 본문 분석 불가”라고 확대 해석하면 안 된다. 다만 모델이 임의 응답 내용을 직접 읽고 새 후속 행동을 만드는 일반 경로는 부족하다. [W13]

### 5.6 구조화 모델 출력 계약의 실제 보장

`LLMClient.complete_structured()`는 schema를 받는 인터페이스다. 그러나 LiteLLM의 일반 `acompletion` 경로는 `response_format={"type": "json_object"}`만 전달하고 `response_schema`를 사용하지 않으며, 결과에도 `json.loads()`만 적용한다. JSON이라는 것과 요청한 구조를 만족한다는 것은 다른 조건이다. [W19] [W20]

별도 ChatGPT provider 경로는 schema를 문자열 지시로 넣지만, 이 클라이언트 안에서 schema validator를 실행하지는 않는다. planner는 선택 ID를 별도로 확인하므로 현재 후보 선택에서 임의 ID가 바로 실행되지는 않는다. 문제는 공통 인터페이스가 약속하는 구조 보장과 구현의 차이다.

Claude의 도구 실행은 입력 `safeParse`, 선택적 `validateInput`, 권한 확인을 거쳐 `tool.call()`로 연결된다. WHS에도 **도구 인자**를 검증하는 `CandidateCompiler`가 있다. 따라서 “WHS는 schema 검증이 전혀 없다”는 평가는 틀리며, 부족한 위치는 모델 응답 공통 경계다. [C03] [W21]

### 5.7 모델 API 오류 복구

Claude `withRetry`에는 제한된 재시도, 오류 종류·호출 출처별 정책, `Retry-After`, 중단 처리가 있다. query에는 너무 긴 입력과 일부 잘못된 모델 출력에 대한 후속 복구 분기도 존재한다. 모든 분기가 항상 활성화되는 것은 아니다. [C08] [C01]

WHS planner는 모델 호출 예외 또는 잘못된 선택을 만나면 결정론적 후보로 fallback한다. 실행을 계속할 수 있다는 장점은 있지만, “모델이 정상적으로 판단했다”는 의미는 아니다. `LiteLLMClient`는 라이브러리 호출을 감싸고 텍스트/JSON만 반환한다. 하위 SDK의 기본 재시도 여부와 별개로, 프로젝트 수준에서 오류 분류·backoff·재시도 이벤트·소모 비용을 관리하는 공통 경로는 확인되지 않는다. [W10] [W20]

특히 `max_retries`는 config/settings와 전달 코드에 있고 `RuntimeState.retry_count`도 있지만, 소스 전체 참조 검색에서 이를 실행 재시도 횟수로 사용하거나 증가시키는 경로가 나오지 않았다. **재시도 설정이 실제 동작을 조절한다고 볼 수 없다.** [W11] [W22]

### 5.8 도구 오류와 재실행 정책

WHS gateway는 timeout과 adapter 오류를 `ToolResult`로 정규화한다. 일부 오류에 `retryable=True`를 표시하지만, gateway는 멱등성을 알 수 없어 자동 재실행하지 않는다고 명시한다. 이는 무조건 결함이라기보다 합리적인 경계다. [W23]

공백은 그 위 계층에 있다. UPDATE는 실패 작업을 FAILED로 마감하고, PLAN은 이미 terminal인 동일 후보를 기본적으로 제외한다. `repeatable` 예외는 있지만 오류 원인별 대기·안전한 재시도·입력 수정·대체 도구 선택의 공통 복구 흐름은 없다. 따라서 일시적 실패도 해당 후보의 종료로 이어질 수 있다. [W03] [W18]

**필요 조건:** 실행 전에 실패한 호출, 결과를 잃었지만 실행됐을 수 있는 호출, 읽기 전용 작업, 상태 변경 작업을 구분해야 한다. Claude의 API 재시도 구현을 보안 도구 호출에 그대로 붙이는 것은 올바른 해결이 아니다.

### 5.9 긴 작업과 백그라운드 수명 관리

WHS `execute_node`는 기본 timeout 10초로 invocation을 만들고, gateway는 semaphore 대기와 실행을 합친 구간에 그 timeout을 적용한다. 실행 lease heartbeat는 실제 구현돼 있다. 다만 heartbeat가 원격 도구 실행 제한을 늘리거나 백그라운드 job으로 바꾸는 것은 아니다. [W24] [W23]

Nmap과 Nuclei 같은 서버는 자체 프로세스 timeout·정리를 구현하지만, 부모 호출의 10초 제한과 별개다. 긴 엔진 작업에서는 상위 호출이 먼저 timeout될 가능성이 있다. 이는 정적 코드에서 도출한 운영 위험이며 실제 시간 초과를 재현한 결과는 아니다. [W25] [W26]

Claude BashTool은 개별 timeout, background 실행, task ID와 출력 조회·중단 도구를 연결한다. WHS에는 일반적인 submit→job ID→status/progress→result→cancel 계약이 없다. [C07] [C06]

**영향:** 오래 걸리는 분석이 “실패”인지 “계속 실행 중인데 응답만 잃음”인지 구분하기 어렵고, 조사와 장기 도구 작업을 병행하기 힘들다.

### 5.10 병렬 실행은 있으나 기본 CLI에서 활성화 범위가 제한된다

WHS는 candidate conflict key, 대상별 동시성, lease, `Send` worker, aggregate, gateway의 서버별 semaphore를 갖춘다. worker는 공유 coordinator 상태와 분리된 local state로 실행하고 정책 입장은 lock으로 직렬화한다. 병렬 실행이 선언뿐이라는 평가는 틀리다. [W02] [W03] [W04] [W23]

다만 `max_actions_per_turn`의 기본값은 1이다. 공용 `OrchestrationConfig`는 `max_steps`, `max_retries`만 노출하며 CLI build는 checkpoint 설정만 추가한다. 기본 CLI 설정 흐름에서 fan-out과 SQLite work backend 같은 내부 옵션을 직접 조절하는 연결이 없다. Python API로 명시적인 `OrchestrationSettings`를 주입하는 경우와 사용자 CLI 기본 동작을 구분해야 한다. [W08] [W11] [W06]

Claude는 도구의 동시 실행 가능 여부로 묶음을 나누고 동시 실행 수를 제한한다. 목표가 다르므로 WHS의 대상·정책 중심 동시성 제어를 버릴 이유는 없다. 먼저 내부 구현을 실제 운영 설정에 연결하는 것이 필요하다. [C02]

### 5.11 역할 분담과 독립 하위 에이전트는 다르다

WHS의 Recon, Web/API, Mobile은 provider/spec 기반 실행 역할이다. Analysis와 Reporting은 `schedulable=False`이며 각각 verifier와 projector 역할이다. 실제로 5개의 독립 모델 에이전트가 협업하는 구조는 아니다. worker 역시 행동 한 건을 policy→execute→verify→update하는 실행자이며 자기 대화, 별도 목표, 반복 추론을 갖지 않는다. [W27] [W04] [W28] [W29]

Claude AgentTool은 별도 문맥과 설정으로 `runAgent()`를 부르고, 그 안에서 같은 `query()` 루프를 실행한다. transcript·metadata·background 상태도 관리한다. [C04] [C09]

**영향:** “웹 분석은 한 에이전트가 여러 번 조사하고, 다른 에이전트는 결과를 독립 재검증한다”는 실제 조사 분업에는 WHS의 단일 행동 worker만으로 부족하다. 기존 scheduler/ledger는 그 기반으로 활용할 수 있으므로 처음부터 다시 만들 필요는 없다.

### 5.12 컨텍스트 투영과 압축

WHS는 대상 주변 그래프, 역할별 node type, 후보 개수와 증거 개수를 제한하고 revision별 cache를 둔다. 이는 유효한 문맥 관리다. 그러나 `max_tokens // 40` 같은 개수 추정이며 최종 prompt 전체를 tokenizer로 측정하지 않는다. 증거 요약은 240자 제한이고, 과거 조사 과정을 모델이 요약해 보존하는 대화 압축과는 다르다. [W30] [W03]

또한 `ContextProfile.include_edge_types`는 역할 정의에 있고 `AgentRegistry.revision_fingerprint()`와 `progress.observation_fingerprint()`에서 역할 재활성화 여부를 판단하는 데 실제 사용된다. 다만 projector 본문은 node 필터와 `build_observation()`을 중심으로 동작하며 edge type 설정을 직접 적용하지 않는다. 따라서 이 필드 전체가 미사용인 것은 아니며, 관계 변화 감지와 planner에게 관계 문맥을 전달하는 기능을 구분해야 한다. [W27] [W30] [W45] [W46]

Claude는 token 추정·모델 context window·압축 경계·요약과 입력 초과 복구를 연결한다. reactive/collapse 등 추가 경로는 feature gate에 따라 달라진다. [C10] [C01]

**영향:** WHS는 작고 정형적인 관측을 다룰 때 효율적이지만, 긴 소스·보고서·여러 차례 추론을 축적하는 조사로 확장할 때 정보 보존과 재검색 장치가 필요하다.

### 5.13 큰 출력의 처리

WHS gateway는 설정된 최대 출력 크기를 넘으면 `output=None`, `tool_output_too_large` 실패로 반환한다. 이 경로에서 원문을 artifact로 저장하고 모델에게 부분 열람할 참조를 주지는 않는다. [W23]

프로젝트에는 별도 `FilesystemArtifactBackend`와 증거 저장 기능이 있으므로 “파일 저장 기능이 없다”는 뜻은 아니다. **일반 도구 실행의 큰 출력 처리와 그 저장·읽기 기반이 연결되지 않았다는 문제**다. [W31] [W32]

Claude의 `toolResultStorage`는 큰 텍스트 결과를 디스크에 저장하고 preview와 참조로 바꾼다. 실패 시 원래 출력을 유지하는 예외와 이미지 같은 별도 취급도 있으므로 모든 종류의 결과를 같은 방식으로 저장한다고 일반화하지 않았다. [C11]

**영향:** 큰 결과일수록 필요한 부분을 찾아 읽을 수 있어야 하는데, 현재 WHS의 일반 경로는 유용한 결과를 읽기 어려워지는 대신 실패로 축소할 수 있다.

### 5.14 체크포인트와 완전 복구

WHS에는 LangGraph memory/SQLite checkpoint, 이력 조회, `resume_from`, filesystem 증거·승인·감사 저장, SQLite work 구현이 있다. 하지만 기본 composition root의 Attack Graph와 event bus는 메모리이고 work backend도 기본은 memory다. 그래프 snapshot 직렬화 코드는 있지만 CLI 재시작 시 이를 복원하는 연결은 발견하지 못했다. [W07] [W11] [W33] [W34]

`resume_from()`는 GraphState만 되감으며 Attack Graph/Evidence Store는 그대로 둔다고 명시한다. 이는 전체 시스템의 시점 복구가 아니다. `redteam run --non-interactive`에서 승인 대기로 종료한 경우, 승인 명령은 상태를 바꾸지만 해당 실행을 CLI로 다시 시작할 수 없다고 사용자 안내에 직접 적혀 있다. [W33] [W06]

control plane의 pending proposal·resolved outcome, budget/rate 상태도 기본적으로 프로세스 메모리에 있다. 따라서 **SQLite 체크포인트가 있다는 이유만으로 프로세스 종료 후 일관된 재개를 보장할 수 없다.** [W35]

Claude에는 transcript 기록, 세션 체인 복원, resume/continue 진입, 하위 에이전트 transcript와 worktree metadata 복구가 연결돼 있다. 그렇다고 외부 도구 부작용을 exactly-once로 되돌리는 시스템이라는 뜻은 아니다. [C12] [C13] [C05]

### 5.15 사용자와 실행 중 상호작용

WHS 대화형 UI는 engagement 설정·수정과 preflight를 지원한다. 실행 중에는 이벤트 출력, 승인 대기, runner stop/cancellation이 있다. 따라서 “대화형 UI도 없고 취소도 없다”는 평가는 틀리다. [W05] [W06] [W33]

그러나 실행 중 새 지시를 동일 run에 넣고 목표를 수정하거나, 모델이 판단에 필요한 질문을 던져 답을 받은 뒤 같은 추론 문맥을 이어가는 공통 도구 경로는 확인되지 않는다. 승인 yes/no와 조사 목적의 질문은 서로 다른 기능이다.

Claude query는 메시지 queue·중단을 처리하고, 도구 풀에 AskUserQuestion이 있다. REPL/QueryEngine과 stream-json 실행 경로도 연결된다. [C01] [C06] [C14]

**영향:** 사용자가 “그 엔드포인트는 빼고 다른 인증 세션으로 비교해”라고 도중에 수정할 때, 현재 WHS는 설정 변경·재실행 중심 운영이 필요하다.

### 5.16 MCP의 지원 범위

WHS는 공식 SDK를 감싼 HTTP/stdio transport, 도구 목록 pagination, allowlist, manifest, operation binding, 입력·출력 schema, 서버별 concurrency/output limit를 구현한다. MCP가 스텁뿐이라는 평가는 틀리다. [W09] [W21] [W23] [W36]

그러나 transport는 명시적으로 session 실패 후 자동 reconnect/replay를 하지 않는다. 공통 포트도 list_tools/call_tool/health_check 중심이다. 서버 알림에 따른 실행 중 목록 갱신, 사용자 elicitation, 연결 인증 갱신, 풍부한 진행 알림의 에이전트/UI 통합은 이 기본 경로에서 확인되지 않는다. HTTP header나 stdio 환경 설정 지원을 인증 기능 전체 부재로 잘못 표현해서는 안 된다. [W36]

Claude client에는 서버 연결·도구 호출·진행·abort/timeout·인증 및 session 관련 처리 분기가 있다. 이 폭은 WHS보다 넓다. 단, 해당 코드 전부의 실동작을 이 checkout에서 검증한 것은 아니다. [C15]

### 5.17 확장 방식과 지식 재사용

WHS는 provider, AgentDefinition, tool catalog, mapper 등의 명시적인 확장 지점이 있다. 새 역할 추가 때 중앙 graph를 고치지 않도록 한 점은 설계상 장점이다. [W12] [W27] [W17]

현재 확장은 개발자가 Python 구현과 등록을 추가하는 방식에 가깝다. 사용자 지침 파일을 발견해 필요한 순간 로드하고, 도구와 함께 실행하며, 작업별 기억을 후속 세션에서 가져오는 범용 skill/memory 실행 체계는 기본 agent 경로에서 확인되지 않는다.

Claude에는 skill markdown loader, SkillTool, 메모리 진입점 및 QueryEngine의 memory prompt 로드 경로가 있다. 전체 plugin 생태계의 설치·갱신 안정성까지 이번 비교에서 판정하지 않았다. [C16] [C17] [C14]

**영향:** 새로운 분석 절차를 문서로 추가하는 것과 실제 에이전트 행동으로 추가하는 것 사이에 현재 WHS는 개발 작업이 많이 필요하다.

### 5.18 비용과 운영 관측성

WHS에는 run/action/invocation/evidence ID, 원인 연결 이벤트, 오류 class, 작업 상태와 종료 사유가 있다. 보고서도 행동 대비 성공 작업·증거·검증 finding 같은 비율을 계산한다. 운영 관측성이 전혀 없는 것은 아니다. [W24] [W29]

LLM 포트는 텍스트/JSON만 반환하므로 사용 token, cache 사용량, 모델별 비용·호출 지연 등을 공통 결과로 올리지 않는다. 실행 예산과 `remaining_budget`은 행동 수 중심이며 모델 비용 예산과 구별해야 한다. [W19] [W20] [W24]

Claude cost tracker는 모델 usage와 비용 상태를 유지하고 session 복구와 연결한다. WHS의 모델 비용 및 fallback 빈도 계량은 보강할 가치가 크다. fallback 이유가 planner 객체 안에 존재하는 것과 durable 이벤트/보고서에 일관되게 노출되는 것은 별개다. [C18] [W10]

### 5.19 정책·승인·범위 통제

WHS는 Scope→ROE→Policy→Rate→Budget→Approval 경로를 코드로 실행하고, 승인된 proposal을 invocation으로 바꾼다. allow/deny operation, 시간 창, 병렬 제한과 가용성 영향 검사가 실제 존재한다. `prohibited_techniques`는 지원하지 않는다는 사실을 명시하고 설정 시 거부한다. [W35] [W37] [W38]

`control/service.py` class docstring에 남은 “ROE pass-through” 설명은 현재 `roe.py` 구현과 맞지 않는다. 주석만 읽고 ROE 미구현으로 판정해서는 안 된다.

Claude의 공통 도구 권한·명령/파일 규칙·sandbox 연동은 로컬 코딩 환경 제어에 맞춰져 있다. WHS의 engagement 정책과 동등한 대체물이 아니다. 반대로 WHS의 action 승인도 임의 코드 실행을 위한 OS 수준 sandbox가 이미 구현됐다는 뜻은 아니다. [C19]

**판정:** WHS가 기능 확장을 위해 이 정책 계층을 폐기할 이유는 없다. 모델 행동 생성 계층을 그 앞에 추가하는 것이 현재 구조를 살리는 방향이다.

### 5.20 검증과 증거에 근거한 취약점 주장

WHS는 ToolResult→EvidenceRecord→verifier→가설 판정→FindingPromoter를 분리한다. security_check 여부, VIOLATION, 연결된 증거, 확인된 가설, 중복 claim 검사를 거쳐 finding을 만든다. HTTP 인증·인가에는 baseline과 세션 관측을 비교하는 별도 경로도 있다. [W39] [W40] [W41]

이는 범용 에이전트의 자연어 결론만으로 대체하면 안 되는 강점이다. 다만 기본 AnalysisVerifier는 선언된 expectation을 평가하는 결정론적 구현이다. 이름이 Analysis Agent라고 해서 다른 모델이 독립적으로 재현 실험을 수행하는 것은 아니다. `tool_succeeded`, field 존재/일치, 문자열 포함만으로 임의 취약점의 인과관계를 검증할 수는 없다. [W28] [W42]

증거 파일 integrity 검사는 저장된 record와 index의 변경 검출이다. 모든 원본 도구 출력의 의미적 진실성이나 독립 재현을 증명하지 않는다. 일반 ToolResult 전체를 원본 그대로 항상 보관하는 구조도 아니다. [W32]

**격차:** 도메인 검증 프레임은 있지만, 조사 가설을 새로 만들고 필요한 재현 방법을 선정하며 추가 증거를 모으는 추론 계층은 제한적이다.

### 5.21 종료 판단과 보고서

WHS는 활동 중 work, 미해결 가설, cleanup을 확인하고 단순 후보 소진과 불완전한 종료를 구분한다. reporting도 이미 canonical 상태에 있는 사실만 투영한다. 이는 가짜 성공을 줄이는 유효한 구현이다. [W43] [W29]

하지만 `ready_for_release`는 현재 조건상 FINISHED + NO_ACTIONABLE_PLAN + integrity/가설/cleanup 통과를 의미한다. **후보가 더 없다는 사실은 보안 목표가 충분히 검증됐다는 사실과 다르다.** 좁은 provider가 생성할 수 있는 후보를 모두 쓴 상황에서도 조사 범위는 부족할 수 있다.

따라서 보고서에는 확인한 범위, 확인하지 못한 범위, 제공되지 않은 도구/입력, 실패한 조사와 결론의 근거를 별도 coverage 기준으로 표현할 필요가 있다. Claude 역시 모델이 완료했다고 응답했다는 이유만으로 실제 목표 달성이 입증되는 것은 아니다. 양쪽 모두 목표별 완료 기준이 필요하되, WHS에는 보안 평가 범위 증명이 특히 중요하다.

### 5.22 선언과 실행 연결이 다른 항목

| 항목 | 확인된 사실 | 잘못 해석하면 생기는 문제 |
|---|---|---|
| `max_retries`, `retry_count` | 설정·상태 선언, 실행 소비/증가 경로 미확인 | 재시도가 설정대로 작동한다고 오인 |
| `AgentResult`, `FollowupHint` | 타입 정의와 export 중심, 기본 노드는 `WorkerOutcome` 사용 | 후속 제안이 자동 실행되는 프로토콜로 오인 |
| Analysis / Reporting | schedulable=False인 seam 역할 | 독립 추론 에이전트 수를 과대 계산 |
| `complete_structured(response_schema=...)` | 일반 경로가 schema를 사용하지 않음 | JSON 유효성을 업무 구조 유효성으로 오인 |
| `ContextProfile.include_edge_types` | 역할 재활성화 fingerprint에는 사용, projector 본문에서는 직접 적용 안 됨 | 변화 감지를 관계 중심 문맥 투영과 동일시 |
| `global_concurrency=4` | `max_actions_per_turn=1` 기본값이 fan-out 제한 | 기본 CLI가 4개 동시 실행한다고 오인 |
| SQLite checkpoint | domain graph/control/work 복원은 별개 | 프로세스 종료 후 완전 복구로 오인 |
| 그래프 snapshot 유틸리티 | 직렬화 함수 존재, 기본 CLI 복원 연결 없음 | 저장 유틸리티 존재를 제품 기능 완료로 오인 |
| ROE pass-through 주석 | 구현은 여러 ROE 조건을 실제 검사 | 주석 때문에 구현 수준을 과소 평가 |

이 표는 선언의 존재와 실제 동작을 분리한 정적 분석 결과다. `[W44]` 타입만 향후 계획용으로 남아 있는 것 자체를 버그라고 단정하지 않는다. 사용자에게 이미 지원한다고 표현한다면 수정해야 할 차이다. [W11] [W22] [W27] [W20] [W30] [W34] [W44]

## 6. 실제 사용 상황으로 본 격차

| 상황 | 현재 WHS에서 기대 가능한 범위 | 부족한 연결 |
|---|---|---|
| 알려진 대상과 도구로 관측·접근 비교 | provider와 계약이 지원하면 후보→정책→도구→증거→판정 | 필요한 target/session/정책 사실이 사전 제공돼야 함 |
| 낯선 저장소에서 취약한 코드 위치 조사 | 등록된 전문 도구와 provider 범위 | 모델 주도 파일 검색·읽기·분석 반복 |
| 도구 인자 오류가 발생 | 실패 기록·정리·다음 후보 또는 종료 | 오류 이해 후 인자 수정과 재검증 |
| 수십 초 이상 분석 엔진 실행 | 서버 자체 실행 기능은 존재 | 상위 timeout, job 추적, 부분 출력, 재연결 |
| 매우 큰 분석 결과를 반환 | 크기 제한 초과를 실패로 기록 | 파일 보관·검색·부분 열람·계획 연결 |
| 승인 대기 중 터미널 종료 | 승인/증거/체크포인트 일부 보존 | 같은 run의 통합 복구와 CLI resume |
| 웹 조사와 독립 검증을 분담 | 여러 후보를 병렬 실행 가능 | 역할별 독립 추론 루프와 결과 재검증 임무 |
| 실행 중 목표 수정 | 설정 대화·승인은 존재 | 지속 실행 안에서 새 메시지와 목표 반영 |

위 표는 코드를 근거로 한 예상 동작 범위다. 해당 시나리오의 실측 성공/실패 결과가 아니다.

## 7. 우선순위와 완료 기준

우선순위는 위험도 등급이 아니라 **기본 동작과 조사 능력에 미치는 영향 및 의존성** 기준이다. 아래는 개선 제안이며 이번 작업에서 구현하지 않았다.

| 순서 | 작업 | 권장 범위 | 완료를 판단할 최소 사례 |
|---|---|---|---|
| 1 | 실행 계약의 불일치 정리 | fake provider 명시화, unknown provider 거부, structured schema 처리, 미작동 설정 설명 | provider 오타가 거부되고 모델 응답이 요청 구조를 벗어나면 명확히 처리됨 |
| 2 | 도구 timeout·출력 보존 개선 | 작업별 timeout, artifact spill, 부분 읽기 연결 | 긴 정상 작업을 구분하고 큰 결과를 잃지 않고 다시 읽음 |
| 3 | 통합 run 복구 | graph/work/control/checkpoint의 저장·복원 및 CLI resume | 프로세스 종료 후 같은 run의 상태·승인·대상·남은 작업을 이어감 |
| 4 | 모델 주도 행동 초안 생성 | 허용 도구 schema로 action draft 생성→기존 compiler/policy 적용 | playbook에 고정되지 않은 도구 인자·순서를 모델이 만들되 범위 검사는 유지 |
| 5 | 조사용 기본 자료 접근 | 검색·부분 읽기·분석 결과 조회, 필요한 제한된 로컬 분석 실행 | 낯선 자료를 찾아 읽고 그 근거로 다음 행동을 고름 |
| 6 | 실패별 복구 정책 | 모델 API/도구 오류 분리, 멱등성·불확실 실행 구분, bounded retry | 일시적 읽기 실패는 회복하고 실행 여부 불명인 변경 작업은 무조건 중복하지 않음 |
| 7 | 문맥과 결과를 연결 | tool result 대화, 중요한 근거 유지, 압축/검색, 전체 prompt budget | 긴 조사에서도 목표·제약·출처와 핵심 관측을 잃지 않음 |
| 8 | 장기 job 관리 | submit/status/progress/result/cancel + 재시작 후 조회 | 긴 분석과 다른 조사를 병행하고 종료 여부·결과를 회수 |
| 9 | 독립 하위 에이전트 | task별 목표·문맥·도구·예산·추론 루프·결과 계약 | 실행자와 검증자가 별도 조사 과정을 수행하고 근거를 반환 |
| 10 | 사용자 상호작용 | 실행 중 질문/답변, 지시 추가, pause/stop/revise | 재시작 없이 현재 조사 목표를 수정하고 변경 이력을 보존 |
| 11 | 운영 설정과 계량 | concurrency/work backend 설정 노출, usage/cost/fallback 이벤트 | 실제 모델 비용·오류·진행과 운영 설정을 보고서에서 확인 |
| 12 | coverage 기반 완료 판단 | 검사 범위·미검증 범위·중단 이유와 목표 충족 기준 | 후보 소진이 전체 보안 검증 완료로 표시되지 않음 |

4번 이후의 핵심 방향은 기존 오케스트레이터를 폐기하는 것이 아니다. **모델이 행동 초안을 만들고, 기존 compiler·scope·ROE·approval·evidence 계층이 실제 실행과 주장 품질을 통제하도록 확장**하는 것이다. 먼저 복구와 자료 접근의 기본기를 보강해야 더 자율적인 계획이 실제 실행으로 이어진다.

## 8. 검증 수준과 남은 불확실성

- 소스의 실제 함수·호출·분기를 대조했다. 폴더명, 클래스명, README의 기능 설명만으로 구현 여부를 판정하지 않았다.
- 재시도 설정, AgentResult, graph snapshot, context edge profile은 전체 `src` 참조 검색으로 선언·사용 차이를 추가 확인했다.
- 관련 테스트 파일 위치를 확인했지만 이번 작업에서는 테스트를 실행하지 않았다. 테스트가 존재한다는 사실을 통과 또는 실제 서비스 검증으로 표기하지 않았다.
- 기존 테스트 예시는 `tests/integration/test_agent_loop.py`, `test_approval_resume.py`, `test_cli_smoke.py`, `test_mcp_stdio.py`, `test_mcp_streamable_http.py`, `tests/unit/test_context_projection.py`, `test_orchestration_work.py`, `test_mcp_session.py` 등이다.
- 문서의 모든 source reference가 실제 파일·심볼로 해석되는지 검사하고, 두 저장소 기준과 변경 범위를 확인했다. 실행 성능·발견률·정확도·비용 배수는 측정하지 않았다.
- 정확한 “Claude 대비 몇 % 구현” 수치는 산출하지 않는다. 기능 선택과 가중치에 따라 임의로 변하고, 정적 소스만으로 실전 성공률을 환산할 수 없기 때문이다.

**최종 판단:** 알려진 절차를 통제하고 결과를 증거로 남기는 기반은 상당히 구축돼 있다. 반면 낯선 문제를 모델이 스스로 조사하는 범용 행동 루프와 오래 실행되는 작업의 제품화 기능은 큰 폭으로 부족하다. 현재 개발의 중심을 도구 개수 확대만으로 잡으면 이 격차는 남는다. 행동 생성·자료 접근·복구·문맥·장기 작업을 연결하는 것이 기능 수준을 올리는 직접적인 경로다.

## 9. 근거 파일

아래 W/C 링크는 기준 checkout의 실제 소스 파일과 시작 행을 가리킨다. W는 WHS, C는 로컬 Claude이다. 절의 판단은 연결된 파일의 해당 심볼 본문과 호출자를 함께 읽어 도출했다. 테스트도 실행하지 않았다.

| 근거 | 파일·시작 행 | 추적 시작 심볼 |
|---|---|---|
| W01 | [WHS src/redteam/orchestration/graph.py:86](<C:/Users/qkrgu/Documents/GitHub/WHS-Red-Teaming-Agent/src/redteam/orchestration/graph.py:86>) | `def build_graph` |
| W02 | [WHS src/redteam/orchestration/router.py:28](<C:/Users/qkrgu/Documents/GitHub/WHS-Red-Teaming-Agent/src/redteam/orchestration/router.py:28>) | `def route_after_plan` |
| W03 | [WHS src/redteam/orchestration/nodes/plan.py:99](<C:/Users/qkrgu/Documents/GitHub/WHS-Red-Teaming-Agent/src/redteam/orchestration/nodes/plan.py:99>) | `async def plan_node` |
| W04 | [WHS src/redteam/orchestration/nodes/worker.py:20](<C:/Users/qkrgu/Documents/GitHub/WHS-Red-Teaming-Agent/src/redteam/orchestration/nodes/worker.py:20>) | `async def worker_node` |
| W05 | [WHS src/redteam/app/cli/commands/interactive.py:95](<C:/Users/qkrgu/Documents/GitHub/WHS-Red-Teaming-Agent/src/redteam/app/cli/commands/interactive.py:95>) | `async def _run` |
| W06 | [WHS src/redteam/app/cli/commands/run.py:171](<C:/Users/qkrgu/Documents/GitHub/WHS-Red-Teaming-Agent/src/redteam/app/cli/commands/run.py:171>) | `async def build` |
| W07 | [WHS src/redteam/app/bootstrap.py:198](<C:/Users/qkrgu/Documents/GitHub/WHS-Red-Teaming-Agent/src/redteam/app/bootstrap.py:198>) | `async def build_application` |
| W08 | [WHS src/redteam/core/config.py:65](<C:/Users/qkrgu/Documents/GitHub/WHS-Red-Teaming-Agent/src/redteam/core/config.py:65>) | `class LLMConfig` |
| W09 | [WHS src/redteam/capability/bootstrap.py:58](<C:/Users/qkrgu/Documents/GitHub/WHS-Red-Teaming-Agent/src/redteam/capability/bootstrap.py:58>) | `async def _build_gateway` |
| W10 | [WHS src/redteam/orchestration/agents/planners.py:160](<C:/Users/qkrgu/Documents/GitHub/WHS-Red-Teaming-Agent/src/redteam/orchestration/agents/planners.py:160>) | `class HybridPlanner` |
| W11 | [WHS src/redteam/orchestration/settings.py:23](<C:/Users/qkrgu/Documents/GitHub/WHS-Red-Teaming-Agent/src/redteam/orchestration/settings.py:23>) | `class OrchestrationSettings` |
| W12 | [WHS src/redteam/orchestration/agents/providers.py:235](<C:/Users/qkrgu/Documents/GitHub/WHS-Red-Teaming-Agent/src/redteam/orchestration/agents/providers.py:235>) | `def default_provider_registry` |
| W13 | [WHS src/redteam/orchestration/agents/web.py:64](<C:/Users/qkrgu/Documents/GitHub/WHS-Red-Teaming-Agent/src/redteam/orchestration/agents/web.py:64>) | `class WebCandidateProvider` |
| W14 | [WHS src/redteam/orchestration/nodes/replan.py:54](<C:/Users/qkrgu/Documents/GitHub/WHS-Red-Teaming-Agent/src/redteam/orchestration/nodes/replan.py:54>) | `async def replan_node` |
| W15 | [WHS src/redteam/orchestration/agents/builtin.py:18](<C:/Users/qkrgu/Documents/GitHub/WHS-Red-Teaming-Agent/src/redteam/orchestration/agents/builtin.py:18>) | `WEB_API_AGENT =` |
| W16 | [WHS src/redteam/capability/servers/analysis.py:101](<C:/Users/qkrgu/Documents/GitHub/WHS-Red-Teaming-Agent/src/redteam/capability/servers/analysis.py:101>) | `def create_server` |
| W17 | [WHS src/redteam/orchestration/observations/mapper.py:115](<C:/Users/qkrgu/Documents/GitHub/WHS-Red-Teaming-Agent/src/redteam/orchestration/observations/mapper.py:115>) | `class ResultMapperRegistry` |
| W18 | [WHS src/redteam/orchestration/nodes/update.py:22](<C:/Users/qkrgu/Documents/GitHub/WHS-Red-Teaming-Agent/src/redteam/orchestration/nodes/update.py:22>) | `async def update_node` |
| W19 | [WHS src/redteam/core/ports/llm.py:12](<C:/Users/qkrgu/Documents/GitHub/WHS-Red-Teaming-Agent/src/redteam/core/ports/llm.py:12>) | `class LLMClient` |
| W20 | [WHS src/redteam/llm/litellm.py:47](<C:/Users/qkrgu/Documents/GitHub/WHS-Red-Teaming-Agent/src/redteam/llm/litellm.py:47>) | `class LiteLLMClient` |
| W21 | [WHS src/redteam/orchestration/candidates/compiler.py:62](<C:/Users/qkrgu/Documents/GitHub/WHS-Red-Teaming-Agent/src/redteam/orchestration/candidates/compiler.py:62>) | `class CandidateCompiler` |
| W22 | [WHS src/redteam/orchestration/state.py:68](<C:/Users/qkrgu/Documents/GitHub/WHS-Red-Teaming-Agent/src/redteam/orchestration/state.py:68>) | `retry_count:` |
| W23 | [WHS src/redteam/capability/gateway.py:84](<C:/Users/qkrgu/Documents/GitHub/WHS-Red-Teaming-Agent/src/redteam/capability/gateway.py:84>) | `async def invoke` |
| W24 | [WHS src/redteam/orchestration/nodes/execute.py:25](<C:/Users/qkrgu/Documents/GitHub/WHS-Red-Teaming-Agent/src/redteam/orchestration/nodes/execute.py:25>) | `DEFAULT_TIMEOUT_SECONDS =` |
| W25 | [WHS src/redteam/capability/servers/active.py:15](<C:/Users/qkrgu/Documents/GitHub/WHS-Red-Teaming-Agent/src/redteam/capability/servers/active.py:15>) | `async def run_nmap` |
| W26 | [WHS src/redteam/capability/servers/nuclei.py:26](<C:/Users/qkrgu/Documents/GitHub/WHS-Red-Teaming-Agent/src/redteam/capability/servers/nuclei.py:26>) | `async def _run_engine` |
| W27 | [WHS src/redteam/orchestration/agents/definition.py:96](<C:/Users/qkrgu/Documents/GitHub/WHS-Red-Teaming-Agent/src/redteam/orchestration/agents/definition.py:96>) | `def builtin_agent_definitions` |
| W28 | [WHS src/redteam/orchestration/analysis.py:33](<C:/Users/qkrgu/Documents/GitHub/WHS-Red-Teaming-Agent/src/redteam/orchestration/analysis.py:33>) | `class DeterministicAnalysisVerifier` |
| W29 | [WHS src/redteam/orchestration/reporting.py:50](<C:/Users/qkrgu/Documents/GitHub/WHS-Red-Teaming-Agent/src/redteam/orchestration/reporting.py:50>) | `class ReportingAgent` |
| W30 | [WHS src/redteam/orchestration/context_projection.py:116](<C:/Users/qkrgu/Documents/GitHub/WHS-Red-Teaming-Agent/src/redteam/orchestration/context_projection.py:116>) | `class ContextProjector` |
| W31 | [WHS src/redteam/evidence/artifacts/filesystem.py:26](<C:/Users/qkrgu/Documents/GitHub/WHS-Red-Teaming-Agent/src/redteam/evidence/artifacts/filesystem.py:26>) | `class FilesystemArtifactBackend` |
| W32 | [WHS src/redteam/evidence/stores/filesystem.py:47](<C:/Users/qkrgu/Documents/GitHub/WHS-Red-Teaming-Agent/src/redteam/evidence/stores/filesystem.py:47>) | `class FilesystemEvidenceStore` |
| W33 | [WHS src/redteam/orchestration/runner.py:79](<C:/Users/qkrgu/Documents/GitHub/WHS-Red-Teaming-Agent/src/redteam/orchestration/runner.py:79>) | `class AgentLoopRunner` |
| W34 | [WHS src/redteam/attack_graph/snapshot.py:45](<C:/Users/qkrgu/Documents/GitHub/WHS-Red-Teaming-Agent/src/redteam/attack_graph/snapshot.py:45>) | `def from_records` |
| W35 | [WHS src/redteam/control/service.py:43](<C:/Users/qkrgu/Documents/GitHub/WHS-Red-Teaming-Agent/src/redteam/control/service.py:43>) | `class DefaultControlPlane` |
| W36 | [WHS src/redteam/capability/adapters/mcp/transport.py:61](<C:/Users/qkrgu/Documents/GitHub/WHS-Red-Teaming-Agent/src/redteam/capability/adapters/mcp/transport.py:61>) | `class _SDKMCPTransport` |
| W37 | [WHS src/redteam/control/roe.py:86](<C:/Users/qkrgu/Documents/GitHub/WHS-Red-Teaming-Agent/src/redteam/control/roe.py:86>) | `def check_roe` |
| W38 | [WHS src/redteam/control/policy.py:38](<C:/Users/qkrgu/Documents/GitHub/WHS-Red-Teaming-Agent/src/redteam/control/policy.py:38>) | `def evaluate_policy` |
| W39 | [WHS src/redteam/orchestration/nodes/verify.py:52](<C:/Users/qkrgu/Documents/GitHub/WHS-Red-Teaming-Agent/src/redteam/orchestration/nodes/verify.py:52>) | `async def verify_node` |
| W40 | [WHS src/redteam/orchestration/findings.py:67](<C:/Users/qkrgu/Documents/GitHub/WHS-Red-Teaming-Agent/src/redteam/orchestration/findings.py:67>) | `class FindingPromoter` |
| W41 | [WHS src/redteam/orchestration/authentication.py:186](<C:/Users/qkrgu/Documents/GitHub/WHS-Red-Teaming-Agent/src/redteam/orchestration/authentication.py:186>) | `async def assess_authentication` |
| W42 | [WHS src/redteam/orchestration/expectations.py:91](<C:/Users/qkrgu/Documents/GitHub/WHS-Red-Teaming-Agent/src/redteam/orchestration/expectations.py:91>) | `def verdict_for` |
| W43 | [WHS src/redteam/orchestration/convergence.py:25](<C:/Users/qkrgu/Documents/GitHub/WHS-Red-Teaming-Agent/src/redteam/orchestration/convergence.py:25>) | `async def evaluate_convergence` |
| W44 | [WHS src/redteam/orchestration/agents/result.py:20](<C:/Users/qkrgu/Documents/GitHub/WHS-Red-Teaming-Agent/src/redteam/orchestration/agents/result.py:20>) | `class AgentResult` |
| W45 | [WHS src/redteam/orchestration/agents/registry.py:125](<C:/Users/qkrgu/Documents/GitHub/WHS-Red-Teaming-Agent/src/redteam/orchestration/agents/registry.py:125>) | `def revision_fingerprint` |
| W46 | [WHS src/redteam/orchestration/progress.py:13](<C:/Users/qkrgu/Documents/GitHub/WHS-Red-Teaming-Agent/src/redteam/orchestration/progress.py:13>) | `def observation_fingerprint` |
| C00 | [Claude CLAUDE.md:5](<C:/Users/qkrgu/Documents/GitHub/claude-code/CLAUDE.md:5>) | `## What this repository is` |
| C01 | [Claude src/query.ts:311](<C:/Users/qkrgu/Documents/GitHub/claude-code/src/query.ts:311>) | `export async function* query` |
| C02 | [Claude src/services/tools/toolOrchestration.ts:19](<C:/Users/qkrgu/Documents/GitHub/claude-code/src/services/tools/toolOrchestration.ts:19>) | `export async function* runTools` |
| C03 | [Claude src/services/tools/toolExecution.ts:761](<C:/Users/qkrgu/Documents/GitHub/claude-code/src/services/tools/toolExecution.ts:761>) | `const parsedInput = tool.inputSchema.safeParse(input)` |
| C04 | [Claude src/tools/AgentTool/runAgent.ts:885](<C:/Users/qkrgu/Documents/GitHub/claude-code/src/tools/AgentTool/runAgent.ts:885>) | `for await (const message of query({` |
| C05 | [Claude src/main.tsx:1198](<C:/Users/qkrgu/Documents/GitHub/claude-code/src/main.tsx:1198>) | `.helpOption('-h, --help'` |
| C06 | [Claude src/tools.ts:196](<C:/Users/qkrgu/Documents/GitHub/claude-code/src/tools.ts:196>) | `export function getAllBaseTools` |
| C07 | [Claude src/tools/BashTool/BashTool.tsx:479](<C:/Users/qkrgu/Documents/GitHub/claude-code/src/tools/BashTool/BashTool.tsx:479>) | `export const BashTool =` |
| C08 | [Claude src/services/api/withRetry.ts:224](<C:/Users/qkrgu/Documents/GitHub/claude-code/src/services/api/withRetry.ts:224>) | `for (let attempt = 1;` |
| C09 | [Claude src/tools/AgentTool/AgentTool.tsx:197](<C:/Users/qkrgu/Documents/GitHub/claude-code/src/tools/AgentTool/AgentTool.tsx:197>) | `export const AgentTool =` |
| C10 | [Claude src/services/compact/autoCompact.ts:190](<C:/Users/qkrgu/Documents/GitHub/claude-code/src/services/compact/autoCompact.ts:190>) | `export function getEffectiveContextWindowSize` |
| C11 | [Claude src/utils/toolResultStorage.ts:283](<C:/Users/qkrgu/Documents/GitHub/claude-code/src/utils/toolResultStorage.ts:283>) | `async function maybePersistLargeToolResult` |
| C12 | [Claude src/utils/sessionStorage.ts:931](<C:/Users/qkrgu/Documents/GitHub/claude-code/src/utils/sessionStorage.ts:931>) | `private async appendToFile` |
| C13 | [Claude src/screens/ResumeConversation.tsx:260](<C:/Users/qkrgu/Documents/GitHub/claude-code/src/screens/ResumeConversation.tsx:260>) | `restoreCostStateForSession(result_3.sessionId)` |
| C14 | [Claude src/QueryEngine.ts:35](<C:/Users/qkrgu/Documents/GitHub/claude-code/src/QueryEngine.ts:35>) | `import { query }` |
| C15 | [Claude src/services/mcp/client.ts:682](<C:/Users/qkrgu/Documents/GitHub/claude-code/src/services/mcp/client.ts:682>) | `export const connectToServer` |
| C16 | [Claude src/skills/loadSkillsDir.ts:294](<C:/Users/qkrgu/Documents/GitHub/claude-code/src/skills/loadSkillsDir.ts:294>) | `export function createSkillCommand` |
| C17 | [Claude src/memdir/memdir.ts:337](<C:/Users/qkrgu/Documents/GitHub/claude-code/src/memdir/memdir.ts:337>) | `export function buildMemoryPrompt` |
| C18 | [Claude src/cost-tracker.ts:145](<C:/Users/qkrgu/Documents/GitHub/claude-code/src/cost-tracker.ts:145>) | `export function saveCurrentSessionCosts` |
| C19 | [Claude src/utils/permissions/permissions.ts:1114](<C:/Users/qkrgu/Documents/GitHub/claude-code/src/utils/permissions/permissions.ts:1114>) | `const canSandboxAutoAllow` |
