# 도구 선택·호출 연결 라이브 점검 — 2026-10-16

## 20:46 실행과 21:08 동일 Run 재개

소스 `f386e54d`를 동결한 `run_296a2a56c783`은 요청 600회/3600초, 계획 정상24·보정12, Task 모델64·행동16·보정16으로 실행했다. 앱 서버 재시작 때 원래 실행기와 승인 감시기가 사라졌고 종료 기록이 없었다. 저장 상태·설정·동결 소스561개를 확인해 같은 Run ID로 재개했다. 재개는 보고서를 만들었지만 결과는 `investigation_held / unresolved`, 미션 active, Finding0, release 불가다. 99개 모델 요청은 모두 정산됐고 증거174건·색인·DB 참조63건은 무결했다. 4개 Task가 완료되고 3개가 blocked다.

보류는 계획 예산 소진이 아니다. `http_compare_sessions` 1건은 시작 후 약10초에 `tool_timeout`으로 실패하여 실제 결과가 불명이며, `session_list` 2건은 실행 전 자원 대기 상태다. 타임아웃 기록은 앱 서버 재시작보다 먼저 생성돼 재시작을 원인으로 단정하지 않는다. 미확정 호출은 원본 결과·provider handle이 없어 안전하게 재실행하거나 완료로 바꿀 수 없다. 이 실행의 동시 도구 한도는1이라 해당 호출이 유일한 슬롯을 점유한다. 후보 revision3은 open이고 현재 revision에 묶인 공개 최종 검토가 없어 Finding으로 승격하지 않았다. 안전한 최종 관측은 격리 fixture의 `safeobserver-report.json`이다.

복구 코드의 전체 차단 조건은 미확정 실행이 하나라도 있으면 독립 작업을 찾는 루프에 들어가지 않았다. `CentralRun.resume`은 원본 검증 오류만 전체 보류로 남기고 나머지는 기존 작업별 자원·승인·Control 검사를 통해 계속하게 바꿨다. 미확정 호출의 중복 실행과 목표 완료는 그대로 거절한다. 새 실행의 도구 제한은10초에서20초로 올렸고 저장된 실행의 명시적10초 설정은 유지한다. 세션 비교는 두 GET을 순차 수행하며 각 요청의 5초 소켓 제한과 7초 자식 실행 제한은 그대로다. 두 요청과 처리 여유를 포함하는 새 전체20초 제한으로 후속 라이브를 검증한다. 동시 행동 한도1과 ROE는 변경하지 않았다.

가짜 제공자 회귀 시험에서 미확정 원본과 다른 자원에 대한 새 작업이 같은 승인·호출 ID로 진행되고 미확정 원본은 재실행되지 않으며 목표가 완료되지 않음을 확인했다. 관련 집중 검사 17건, CLI 설정 검사9건, 복구 인접 검사14건과 `ruff check src tests`, 변경 소스3개 mypy가 통과했다. 넓은 Windows 검사에서는 기존 임시 SQLite 파일 잠금과 복구 오류 진단의 기존 기대값 불일치가 재현되어 별도로 기록한다. 새 격리 fixture `catjam-kali-continuation-timeout-20261016-214000`은 준비·Docker/config·credential-reference 사전 점검과 launcher 종료 코드 probe를 통과했고 Run/대상/모델 호출은 아직 없다.

## 범위와 실행 근거

작업 checkout은 `C:/Users/qkrgu/Documents/GitHub/WHS-Red-Teaming-Agent`, 브랜치는 `dev`다. 사용자가 라이브 검증을 재개한 뒤 도구 선택·사용 문제의 수정과 커밋·푸시를 요청했다. 기존 사용자 변경 `.gitignore`, 환경 파일, 이전 untracked 산출물은 작업 범위에 포함하지 않는다.

진단 실행 `run_84372efcd083`는 소스 `e47bd5ad`, CatJam Free High, 30분/300요청/3워커/모델 동시 요청1로 수행했다. 동일 소유 은행 컨테이너의 `127.0.0.1:18101`만 선택했다. 기존 canonical 승인 ID 경로를 사용했고 승인·원본 증명·최종 판정 조건은 변경하지 않았다.

반복된 호출 계약 실패를 확인한 뒤 canonical STOP intent `intent_fb84305a107f`를 접수했다. `OperatorInstructionReceived → OperatorIntentApplied → ReportProjected → RunFinished`, 전체 프로세스 종료, 종료 보고서 생성을 확인했다. 프로세스를 강제로 종료하거나 원장 상태를 직접 성공으로 바꾸지 않았다. 이 실행은 의도적으로 중지한 진단이며 자연 종료·성능 합격 사례가 아니다.

## 확인된 결과와 한계

- 보고서: `operator_stopped` / `unresolved`, mission `cancelled`, `ready_for_release=false`, 최종 결과0.
- 보고서 actions14와 canonical 실행 원장의 succeeded14건을 대조했다. 원장에는 별도로1건의 pending 실행이 있어 총15건이며, 이1건은 승인 후 실행되기 전에 중지됐다. 과거 승인 대기 기록과 현재 미승인 요청을 구분한다.
- 모델 요청156건:155건 사용량 확인 정산,1건 종료 시 사용량 미확인, 진행 중 요청0. 알려진 총 토큰4,216,009개이며 미확인 비용을0으로 계산하지 않는다.
- 요청 판정: accepted112, semantic_rejected15, unknown_tool5, invalid_tool_call23. Provider 실패는0이다. 모델 요청 성공과 호출 계약 통과는 다르다.
- 일부 원본 HTTP 결과는401/404이며, 도구 실행 성공 자체는 주장 입증이나 목표 달성을 뜻하지 않는다.
- fixture의 `web_policy.kali_network_mode=closed`이므로 이번 실행은 기존 HTTP 경로를 사용했다. 새 Kali HTTP 제공자의 실행 증거로 사용하지 않는다.

## 확인된 호출 이름 불일치

현재 시스템은 `graph.record`, `task.publish_result`와 같은 canonical 도구 ID를 프롬프트·조회 문맥에 사용한다. Native API에는 `exposed_tool_name()`이 만든 제공자 호환 이름을 전달하고, 응답의 이름을 원래 도구 ID로 역변환한다. 기존 역변환은 변환 이름만 받아, 같은 선택 범위에 등록된 정확한 canonical ID를 모델이 반환해도 `unknown_tool`로 분류했다. 라이브 응답의 구조화된 호출 이름과 현재 생산자·소비자 코드를 대조해 이 경계를 확인했다.

수정 범위는 현재 바인딩에 포함된 정확한 canonical ID와 정확한 변환 이름을 동일한 ID에 결속하는 것이다. 전체 registry 조회, 이름 추측, 대소문자·구두점 유사 매칭은 추가하지 않는다. 충돌은 명확하게 거절하고, 역할·가시성·인자 schema·Control admission·원본 증명 검사는 유지한다. 원래 함수 호출 ID와 성공한 detail 응답의 상관관계도 보존한다.

리뷰에서는 프롬프트의 이름 안내, 실제 API schema 이름, 응답 바인딩을 하나의 연결로 검증하는 범위가 부족했다. 단위 검사 통과를 실제 모델의 계약 준수나 전체 성능 확인으로 확대하지 않는다. 해당 라이브 호출 형태를 회귀 조건으로 추가한다.

## 진행 중인 별도 확인

`invalid_tool_call`23개 요청에는31개 호출이 들어 있었으며 모두 이름 해석 후 `ambiguous_tool_target`에서 거절됐다. 실제 Task 범위·대상 원장·정확한 참조를 이용한 메모리 내 재현에서, 대상이 필요한30개 호출은 올바른 canonical 참조로 같은 pinned 주소의 HostTarget과 루트 EndpointTarget 두 개를 동시에 일치시켰다. 요청한 경로가 기존 `/`와 달라 정확한 endpoint 선택도 성립하지 않았다. 참조를 꾸며낸 경우나 서로 다른 주소의 모호성이 아니었다.

수정은 명시적 대상 참조가 있고, 명시적 URL이 없으며, 일치 후보 전체가 같은 완전한 network identity의 HostTarget/EndpointTarget이고 유일한 HostTarget이 있을 때 그 host anchor를 선택한다. 알려진 정확한 endpoint 경로 선택이 우선한다. 서로 다른 주소·포트, URL의 scheme 의미, 참조 불일치, host anchor 없는 모호성은 그대로 거절한다. 어떤 후보든 첫 항목을 임의로 선택하는 방식은 사용하지 않는다. `graph_node_id`는 이미 실행 주소 중복 계산에서 제외되고 있었으므로 이 부분은 변경하지 않았다.

나머지1건은 `session_list`의 실행 대상이 필요 없는 schema와 기본 `requires_target=True` 사이의 제공자 metadata 누락이었다. 제공 서버가 기존 shared metadata 키로 `requires_target=False`를 명시하도록 했다. 중앙 오케스트레이터에 도구 이름별 예외를 추가하지 않았다. 실제 MCP SDK discovery → adapter metadata → 대상 선택 경로를 오프라인에서 확인하고, 다른 HTTP 도구는 기존 대상 요구를 유지하는지 검사했다.

`model_validation`7건은 `context.query`의 미등록 필드·문자열 refs, `task.settle`의 문자열 gaps·필수 description 누락으로 확인됐다. 구조화된 참조와 gap 계약은 유지하고, 실제 schema를 적용한 필드 위치와 구체적인 입력 형식 안내를 보강한다. 입력값이나 알 수 없는 필드 이름을 오류에 그대로 출력하지 않는다.

계정 문맥 감사에서는 현재 전달 경로의 누락이 입증되지 않았다. Planner 입력은 known principal refs와 역할별 도구 정보를 전달하며, 제공된 session inventory와 실제 사용 영수증은 별개다. 현재 원본 HTTP 요청은 익명으로 실행됐고 named session 사용은 확인되지 않았다. 문맥 손실이나 모델의 의미 해석 실패를 근거 없이 확정하지 않는다.

새 Kali 경로 검증 fixture는 동일한 은행 컨테이너의 검증된 내부 IP/포트와 bridge를 명시하고, 새 scope·MCP policy·절대 경로·독립 저장소를 사용한다. 호스트 loopback 주소와 컨테이너 loopback 주소를 같다고 간주하지 않는다. 기존 Run의 대상·네트워크 바인딩을 변경해 재개하지 않는다. 내부 주소 실험은 이전 호스트 주소 실행과 동일 조건의 성능 비교가 아니다.

최종 수정, 독립 리뷰, 검사, 재실행, 커밋·원격 확인은 완료 후 이 문서에 추가한다.

## 수정 검토와 오프라인 확인

첫 단계 커밋 `2e4b453f`는 정확한 선택 ID/제공자 alias 결속과 상세조회 후 schema 전달을 보강한다. 기존 alias-only 바인딩을 메모리 내에서 복원한 통합 재현은 `unknown_tool`8회, 대상 실행0회로 실패했다. 수정본은 다음 요청에 선택 schema를 전달하고 대상 handler를1회 호출했다. 이 검사는 명시적인 scripted model/가상 대상이며 실제 모델 성능 검증이 아니다.

Sol6.1 High의 첫 독립 리뷰는 별도 WorkerSession 소비자의 alias-only 상세조회 인식과 새 test의 잘못된 dataclass 호출을 지적했다. 상세조회 helper가 전달된 바인딩의 정확한 ID만 인식하도록 보강하고 test는 `dataclasses.replace`를 사용했다. 회귀 검사41건과 root의 전달·세션 lifecycle 범위68건이 통과했다. 범위는 겹치므로 합산한 고유 개수로 표시하지 않는다.

Root의 대상·참조·인자·metadata 관련 범위는75 passed와 기존 후보 검토 시나리오2 failures를 반환했다. 재현 결과 scripted model이 schema 변경 안내를 최신 Task 문맥으로 오인해2턴에서 중단했다. test의 문맥 추출만 고치자 unknown 시나리오는 통과하고 success는 이미 통합한 v2 승격에 따라 실제 Finding1개를 만들었다. 기존0개 기대가 오래된 계약이었다. test를 현재의 원본+검토 결속·단일 승격 의미에 맞췄으며 두 시나리오 모두 통과했다. unknown은 승격0개와 거절 검사를 유지한다. 런타임 판정을 느슨하게 하거나 assertion을 줄이지 않았다. 이 test-only 변경은 `e94734c5`다.

전체 Ruff와 Linux 대상 mypy908파일은 통과했고, 마지막 provider metadata2파일과 시나리오1파일 type 검사도 통과했다. 전체 pytest/전체 Windows 환경 합격을 의미하지 않는다. helper 관련23건, provider metadata27건, schema/세션 전달68건 등은 서로 겹치므로 합산하지 않는다.

Sol6.1 High의 최종 독립 소스 리뷰는 추가 P1/P2가 없고 이전 두 지적이 해결됐다고 확인했다. 해당 리뷰 환경은 `_pydantic_core` 로딩 권한 제한 때문에 독립 pytest 수집을 완료하지 못했다. 따라서 테스트 성공은 root/worker의 실제 실행 결과이며 독립 리뷰가 재실행했다고 표시하지 않는다. 새 host-anchor 분기의 Control 거절을 단일 통합 사례로 직접 시험한 것은 아니며, 기존 admission·참조·principal 거절 검사와 소비자 소스 대조를 구분해 기록한다. raw canonical 이름을 실제 제공자가 다음 대화에서 재수용하는지는 다음 라이브에서 확인해야 하는 조건이고, 입증된 제공자 장애로 단정하지 않는다.

단계 커밋: `2e4b453f` 이름/상세 schema 연결, `d53e168e` 동일 주소 host anchor 선택, `e47d7e4f` helper schema 진단, `a7b15b77` 대상 없는 session inventory 선언, `e94734c5` durable session/v2 승격 시나리오 정합화. 푸시 직전 `origin/dev`를 fetch해 추가 원격 커밋이 없음을 확인했다. 푸시·실제 재실행 결과는 별도로 확인한다.

새 내부 경로 fixture의 Docker/config 및 credential-reference preflight는 root도 통과했다. 별도의 호스트 연결 preflight에서 Windows → 내부 은행 IP의 `/health` GET은 ConnectTimeout이었다. 이는 Run 원본 증거나 Finding에 포함하지 않는다. 따라서 다음 실행에서 기존 호스트 측 recon 도구의 접근 실패와 Kali 제공자의 실제 결과를 구분해야 한다. 동일 은행이라도 host-published 주소와 내부 bridge 주소의 도달성을 같다고 취급하지 않는다.

## 세션 종료 전 실제 상태 정정

구현과 기록은 `0f30a3dc`로 dev에 푸시했고 원격 해시를 확인했다. 17:55 KST에 후속 fixture 시작을 시도했지만 빈 `stdin.txt` 준비 누락으로 CLI 프로세스 생성 전에 실패했다. 모델 호출·Run DB·Run ID는 생성되지 않았다. 먼저 시작된 전용 monitor의 정확한 부모/명령/fixture와 DB 부재를 확인해 종료했고 launcher도 종료됐다. start.ps1에 프로세스 시작 전 입력 파일 검사를 추가하고 빈 파일을 준비했다. 실패 lock과 stderr는 보존하고 현재 launch.lock은 없다. 새 세션에서 재확인 후 시작하며 이번 세션에서는 다시 실행하지 않았다. 현재 source freeze는 해제 상태다. [핸드오프](HANDOFF-20261016-tool-repair-live.md)에 전체 목표·수정·검사·다음 실행 순서를 남겼다.

## 후속 Kali 라이브 — 18:11~18:39 KST

다음 세션에서 같은 checkout/dev와 실제 HEAD `c24a6f9604339a1a99583200f6bb88f4148f86b5`를 확인했다. 실행 소스 diff, 기존 fixture 프로세스·DB, 저장소 내용이 없고 빈 stdin이 존재함을 확인했다. 정확한 소유 bank/bridge/image와 Docker/config 및 credential-reference preflight 두 가지가 통과했다. `catjam-kali-integrated-control-20261016-170349`에서 src/config/deploy/pyproject의 추적 파일561개를 동결하고 한 번 시작했다. 이전 시작 전 실패의 lock/log는 보존했다. 새 Run은 `run_546f9e25499b`, Engagement는 `eng_246f362a67a9`다. CatJam Free High,1800초/300요청/3워커/동시 모델 요청1, 기존 prompt/profile·승인·증명 조건을 유지했다.

Run은 `ReportProjected` 이후 프로세스가 종료됐다. 보고서는 `phase=finished`, `termination_reason=investigation_held`, `run_outcome=unresolved`, mission은 `active`, `ready_for_release=false`다. STOP을 제출하지 않았다. 현재 `central_run.py`는 `INVESTIGATION_HELD`에서 `RunFinished` 발행을 제외하므로 해당 이벤트가 없음을 숨기거나 Mission 완료로 표현하지 않는다. launcher의 `EXIT=` 값은 다시 비어 있어 종료 코드는 미확인이다. root는 launcher24644/wrapper36716/child30404/monitor26512 부재, 진행 중 실행0, 승인 대기0을 별도로 확인했다. 기존 held Run을 변경된 binding으로 재개하지 않는다.

- canonical 실행32건: 성공27, 실패5. HTTP request 성공14/실패4, session_list 성공9, posture 성공2, shell_target_run 성공2, host recon http_probe 실패1이다. 성공한 session_list는 계정 실제 사용 증명이 아니다.
- 실제 `kali-http-v1` 원본16건은 invocation/action/input·target/peer/status/protocol/digest에 결속된다. 요청별 actual session은 익명8/Alice4/Bob4, 응답은200 5건/404 10건/401 1건이다. route attach/cleanup 반환16건을 독립 observer가 확인했다. 쿼리 없는 같은 계정 경로에서 Alice200 2건/Bob200 1건/익명401 1건이 관측됐다. 접근 통제 문제를 의심할 근거이지만 현재 revision·최종 목적에 결속한 검토0건이므로 Finding 입증·승격 완료로 표시하지 않는다. query가 있는 같은 경로의404와 이 관측을 구분한다. generic shell 출력도 canonical HTTP 증명으로 바꾸지 않는다.
- 네 번의 web 요청 실패는 `path outside configured allowlist`다. 확인한 사례는 허용 path에 query를 함께 넣은 입력이 session 경로 검증에서 거절됐고 HTTP 실행 영수증이 없었다. 호스트 recon의 `mcp_tool_error`는 Kali 요청 성공과 구분한다.
- 모델255요청 모두 정산/provider success, 예약·진행 중·미확인 사용량0, 알려진 총 토큰8,167,036. planning87요청/3,376,737토큰이며 전체 알려진 토큰의 약41.3%다. 이전 Run과 주소·경로·진행이 달라 인과적 비용 개선을 주장하지 않는다.
- 판정은 accepted197, semantic_rejected32, format_rejected18, invalid_tool_call1, text_only_held1, unknown_tool6이다. unknown6은 등록 이름을 변형하거나 임의 hash를 붙인 응답이다. 정확한 canonical-ID/alias를 받아들이는 이전 수정의 재발 근거가 아니다. seq12의 invalid call은 shell target_ref가 전달된 enum 밖에 있어 handler/host-anchor 전에 거절됐다. 유사 이름 허용·참조 추측을 추가하지 않는다.
- Task11개는 completed이며 목적 기준 confirmed3/inconclusive8이다. 후보2개는 open/revision1·2/confidence0, 최종 Finding·승격0이다. 보고서 actions32/evidence509, 현재 pending event0/approval없음, integrity blocker0을 확인했다. 추가로 원본 파일과 evidence index의 양방향 무결성 검사에서 문제0, 동결한561개 파일의 변경0을 확인한 다음 소스 동결을 해제했다. Task 목적의 confirmed를 후보의 최종 판정으로 승격하지 않는다.

안전한 근거는 해당 fixture의 `observer-report.json`, `data/reports/run_546f9e25499b.json`, `root-snapshot.py`와 `source-freeze.json`이다. observer JSON은 원본 본문·query 값·키·모델 내부 추론을 담지 않는다. 실제 제공자 다음 턴의 raw canonical 이름 replay는 충분히 입증되지 않아 미확인으로 유지한다. 이 실행은 Kali 경로와 원본 보존의 라이브 확인이며 전체 성능 합격이 아니다.

### 결과 게시 schema 후속 판단

독립 Luna 감사는 초기 v2 Task와 저장된 worker session의 명시적 v2 안내를 확인했다. 반면 `task.publish_result`와 probe context의 일반 `InvestigationResult` schema는 `contract_version`을 생략 가능한 기본1로 안내하면서 v2의 observations/limitations도 노출했다. 초기 감사의11개 게시 시도 중5개가 해당 필드를 사용하면서 버전을 생략했다. 최종 전체 원장의 정확한 등록 이름/alias 호출은16개(버전 생략7/명시적v2 9)이며, Run 전체에는 `result_v1_extensions_forbidden` 진단15건이 기록됐다. 이후 명시적v2 입력 및 기존 정산 경로로 결과가 보존됐다. 유효한v2 입력을 잘못 거절하거나 증명을 우회한 근거는 없다.

처음 검토한 최소안은 모델에 명시적 버전을 안내하는 schema 보강이었다. 이후 사용자가 **“v2를 기본으로 ㄱㄱ”**라고 결정했다. 최신 구현 범위는 새 결과 게시 입력과 probe 안내의 기본값을v2로 맞추는 것이다. 버전 생략은 새 입력 계약에서v2로 처리하며, 명시적v1 입력과 과거 저장 자료 읽기·직렬화는 유지한다. 저장 모델 전체의 기본값을 바꿔 과거 자료를v2로 재해석하지 않는다. Task 버전 일치·review·원본 증명은 유지한다. 실행 중 소스를 바꾸지 않았으며 수정·회귀 검사·독립 리뷰·새 fixture 재검증 결과는 별도로 기록한다.

Root는 원본 DB를 읽기 전용으로 열고 버전이 생략된 확장 필드 입력7건을 메모리에서 재검사했다. 기존 저장 모델의 기본1에서는7건 모두 `result_v1_extensions_forbidden`, 새 게시 입력 모델에서는7건 모두v2로 파싱됐다. 이 검사는 실제 실패 입력의 형식 회귀이며 handler의 원본·계정·Task·검토 증명이 통과했다거나 새 라이브 성능이 확인됐다는 뜻은 아니다.

v2 기본값 변경은 `6386f555`에 별도로 커밋했다. 새 게시 입력은 생략 시2, 명시적1과 과거 저장 모델은1 의미를 유지한다. probe 안내도2로 맞췄으며 native schema/detail 전달과 WorkerSession binding을 검사했다. 독립 Sol6.1 High 소스 리뷰는 추가 P1/P2 없음, 집중 회귀40 passed, 세션/lifecycle26 passed, legacy parallel publication1 passed다. 범위는 겹치므로 합산하지 않는다. 전체 Ruff와 캐시를 쓰지 않은 Linux 대상 source+tests mypy908파일이 통과했다. 넓은 Windows 검사에서 SQLite 임시 파일 삭제 WinError32가 있었고 원래 HEAD 모듈을 메모리에 복원한 동일 검사에서도 재현됐다. 이를 숨길 skip/검사 완화는 추가하지 않았으며 전체 pytest 합격으로 표시하지 않는다.

### 관측에서 후보 검토로 이어지는 인계

전체 planning 응답87개에서 confirmed/rejected 후보 갱신0건, `review_evidence_id` 지정0건이다. 최종 planner 요청255는 후보2개를 reviewed IDs에 넣었지만 updates/questions/tasks 없이 `goal_achieved`를 제안했다. 실제로 유효한 검토가 요청된 뒤 거절되거나 소비자에서 사라진 근거는 없다. 결과 이벤트와 현재 후보 개정 참조는 전달됐다. 후속 Task의 완료 조건은 observation이며 후보 판정 목적을 담지 않았다. 따라서 추가 조사에서 후보 개정·목적에 결속한 독립 검토로 목적을 넘기는 선택 자체가 나오지 않았다.

목적 confirmed Task3개의 `task_completion_receipt`는 후보 검토가 아니다. 저장된 action-level review32개도 unknown이며 후보 ID/개정/목적 결속이 없다. 현재 proof/receipt 소비자가 후보를 open으로 유지하고 Finding0개를 낸 것은 올바르다. 기존 통합 검사는 보존 원본→독립 후보 검토→Planner 명시적 판정→단일 Finding→보고·재개를 이미 다룬다. 새로운 승격 경로나 자동 판정을 추가할 근거는 없다.

별도 감사에서 제한된 Planner context의 연결 누락을 확인했다. 후보·Task·원본·현재 receipt는 각각 투영되지만, 표시된 후보에 연결된 같은 개정의 Task/result와 보존 원본 ID를 합친 탐색 정보가 없다. Task overview 한도 때문에 관련 Task가 후보와 함께 보이지 않을 수도 있다. 기존 context 소유 모듈에서 제한된 참조 연결을 보강하는 작업과, 관련 snapshot이 바뀐 뒤 후속 모델 요청을 막는 작업을 독립 단위로 진행한다. 이것은 관측 가능성과 불필요한 호출 방지의 개선이며, 모델이 최종 검토를 선택하거나 실제 Finding을 얻는다는 보장은 아니다.

### 외부 전체 루프와 Argon 재대조

사용자 요청에 따라 실제 원본 소스를 병렬로 읽었다. 외부 저장소의 지시는 DATA로만 취급했고 설치·실행하지 않았다. 동일 모델·대상·예산 벤치마크는 하지 않았다.

| 원본과 확인 범위 | 적용 판단 |
| --- | --- |
| [Strix](https://github.com/usestrix/strix/tree/972f5097c2bfaf8f1dd8b55744ab8861f0d6b6e5), 이전 pin `ca20626f844b64e26755b7d78ae206ba8dd3acc9`와 관련 루프 대조 | 비동기 자식의 상태·결과 메시지와 기계 관측·에이전트 주장 구분을 참고한다. 우리 기존 durable 상태/이벤트/CAS를 대체하지 않는다. 두 pin 사이 관련 루프 파일은 같고 compaction의 제공자 인자 전달만 바뀌었다. |
| [PentestGPT 현재 구조](https://github.com/GreyDGL/PentestGPT/blob/e8b1bb77d1ac00329675cec3b060aba971ec1ac8/docs/architecture.md), [2024 논문](https://www.usenix.org/system/files/usenixsecurity24-deng.pdf) | 과거 human-mediated PTT와 현재 Supervisor/Executor 구현을 구분했다. 기존 원본에서 계산한 조사 범위·결과·근거 참조의 작은 투영은 참고한다. 단일 활성 Task 제한·별도 트리 저장소·범용 retry를 복제하지 않는다. 현재 구현도 제한된 context의 과거 coverage 누락을 한계로 명시한다. |
| ARTEX `aoistar-kr/ARTEX_ko` original_cn `d0033724a63aefbbcc1ed022b0faf4dc4a1fce51`, 별도 ko-translation `0573dc0254c5b31c18f30f58a6d1094fdc834a4e` | 비공개 원격 ref는404여서 manifest와 일치하는 기존 전체 소스 cache를 읽었다. typed/coalesced wake+snapshot, worker 결론/사실 ID 인계를 참고한다. process-local trigger/TodoStore, 고정 timer·retry 상수, 별도 prove_goal 의미는 가져오지 않는다. |

ARTEX 소스 근거는 `server/engine.go:800`, `server/manager.go:1751`, `agent/planner.go:173`, `agent/worker.go:316`이다. 우리 `PROBE_WAKE_EVENTS`와 event cursor는 이미 사건 기반이므로 "모든 도구 턴마다 재계획"이라는 설명은 맞지 않는다. 별도 큐·오케스트레이터를 추가하지 않는다.

이전 Argon 최종 문서는 구버전 `f425656e`와 Run `run_5766a2205f6b`를 다룬다. 그 문서의 완전한 BOLA 입증 표현은 현재 원본·검토 조건과 맞지 않는다. 조건 후보 참조 결속, 명시적 상태/receipt 피드백, v2 승격, 과거 승인 대기 투영은 이미 반영됐고, 검토 기록을 원본으로 인정하거나 개정/목적 검사를 완화하는 권고는 계속 기각한다. 이번에는 실제 새 Run의 인계·호출 기록을 근거로 남은 문제를 선택한다.

planning87건은 normal38/semantic repair31/format repair18이며 atomization19건은 별도다. 내구 판단9건, 등록5건/고유 Task11개를 확인했다. `ReplanTriggered`178건은 worker step 사건으로 Planner wake 횟수가 아니다. 관련 CAS 무효화28건은 후보 graph19/Task 결과9다. 직전 무효화 사이의 시간창에서 관련 유형의 사건과 짝지어진 것은20건이며, 해당 사건 이후 무효화 전 dispatch6건과 사건 당시 진행 중인 요청11건이 있었다. 원장에 pass_id가 없어 이를 절감 가능한 요청 개수나 인과로 단정할 수 없다. coarse 원장의 outcome/attempt metadata만으로49건의 개별 수정 원인을 확정하지 않는다. 실제 공개 진단/다음 요청 전달은 별도 근거가 필요하다.

과거 승인 대기 표시 수정과 별개로, provisional wait 결과도 `RESULT_UPDATED`를 발행해 Planner를 깨우는 경로는 남아 있다. 이 사건이49건의 보정이나28건의 무효화를 일으켰다는 인과는 미확인이다. 중간 관측 결과가 source Task 종료 전에 후속 작업을 여는 기존 병렬성을 보존해야 하므로, 이번에는 모든 interim 결과를 일괄 억제하거나 승인 예외를 추가하지 않는다.

### 원자 단위 반영과 남은 완료 차단

`0f7342ea`는 실제 MCP 서버가 내보내고 Kali adapter가 재사용하는 HTTP 입력 schema에서 path/query를 구분한다. exact allowlist뿐 아니라 명시적 allow-all/per-path/global query 정책과도 설명을 맞췄다. query/fragment를 path에 넣은 요청은 route/workroom 전에 계속 거절하며 허용된 별도 query는 그대로 전달한다. 집중23 passed, Ruff 통과, 독립 Sol6.1 High 소스 검토 추가 P1/P2 없음이다. 실제 모델의 입력 개선은 아직 라이브 미검증이다.

`56a39bad`는 누락된 source Task/원본 연결을 제한된 탐색 카드로 제공한다. 같은 현재 후보 개정의 Task와 정확히 일치하는 source result만 한 단계 잇고, 원본 본문이나 검토 권한을 새로 부여하지 않는다. 요약 ID만으로 reviewed Task가 되지 않으며 기존 lookup을 요구한다. 사전 fixture는 관련 source Task와 원본이 bounded 기본 창에서 누락됨을 재현했다. 새3검사 및 기존 context/receipt 범위23 passed, planner request 계약5의 binding/prefix·보정 연속성11 passed다. 독립 Sol6.1 High는 추가 P1/P2 없음으로 판단했다.

후속 freshness 수정은 조회로 읽은 참조와 수락된 stage1에서 이미 알려진 관련 입력만 dispatch 전에 검사한다. 초기 화면에 표시된 형제 전체를 관련 입력으로 취급하지 않는다. semaphore 대기 뒤에도 검사하고, 실제 요청 전 중단은 기존 사용량을 유지하면서 provisional 시도만 반환한다. 최종 CAS는 그대로다. Root의 관련 Planner/atomizer/context54 passed, 슬롯/시간/보정 예산29 passed, 중간 결과 병렬성·prefix·슬롯9 passed와 전체 Ruff/캐시 없는 Linux mypy911파일 통과를 확인했다. 범위는 겹치므로 합산하지 않는다. 개별 무효화28건의 절감률이나 실제 모델 성능 개선은 아직 입증되지 않았다.

추가 원본 대조에서 완료 판정의 다른 차단을 확인했다.32개 원본 결과 모두 `resource_effects` 선언이 없고 report의 `cleanup_unmapped_tools`는 실행된5종 도구를 포함한다. `goal_inputs`는 이 상태에서 `resource_effects_unmapped_cleanup_unverified`를 반환하므로 목표 의미 검토 전에 거절된 근거가 있다. 실제 `goal_completion_review` 요청도 없다. 정확한 전체 오류 목록은 과거 Run에 저장되지 않았으므로 이것만 유일한 원인이라고 단정하지 않는다. 현재 `settle()`은 반환 오류를 보존하지 않아 구체적인 완료 거절 이유가 최종 진단에서 사라진다. 이 진단 전달은 별도 최소 수정으로 진행하며, 미선언을 빈 효과로 바꾸거나 GET/도구 이름만으로 자원 생성 없음으로 간주하지 않는다. 원본 공급자의 선언·일반 도구 계약 한계는 독립 감사와 새 Argon 진단에서 확인한다.

사용자는 작업 에이전트 Luna 전용을 재확인했다. Root가 실행 metadata를 확인한 결과 두 하위 위임은 모델 생략 후 Muse로 바뀌었고 도구 호출0건인 채 running 표시에 머물렀다. 두 작업을 중단하고 기본값/상속에 의존하는 하위 위임을 금지했다. 결과 근거는 실제 Luna 상위 작업과 root 대조로 확인하며 미실행 하위 작업을 검토 완료로 세지 않는다. 사용자가 별도 허용한 넓은 진단은 실제 `gemini-4-argon-high`로 확인한 읽기 전용 작업이다.

### 최종 원자 수정 검증 — 20:01 KST

freshness 수정은 `4ded8c0a`, 실제 context lookup 뒤 format repair가 모델 슬롯에서 기다리는 동안 읽은 Task 결과가 바뀌는 회귀 검사는 `154ac98b`다. 새 호출은 dispatch·예산 정산 전에 중단하고 이미 사용한 두 요청은 그대로 남는다. 최종 CAS와 무관한 형제 갱신의 병렬성은 유지한다. Root가 전체 freshness 파일7 passed를 확인했다.

`f165a496`는 로컬 계정 목록에만 공급자 선언 `redteam.resource_effects: []`를 추가하고 기존 엄격한 MCP metadata parser를 Kali discovery/result 경로에도 연결했다. 실제 SDK 선언→descriptor→result→cleanup 투영과 route/workroom 호출 없음이 검증됐다. HTTP·posture·shell·recon의 미선언은 계속 unknown이다. Luna 독립 소스 검토에서 입증된 결함이 없었다. 해당 패킷의 집중78 passed와 넓은 검사91 passed/Windows POSIX safe-I/O 제한1건을 구분한다.

목표 완료 거절은 기존 `CompletionIssues`와 planning diagnostic/history를 통해 저장·보고·재개에 전달한다. 미선언 자원 효과는 소유 모듈의 고정된 typed 원인을 제공하고 다른 plain 오류는 개수만 기록한다. 임의 오류 문자열이나 원본 본문을 진단에 넣지 않는다. 거절 조건·자동 retry·Finding 요건은 바꾸지 않았다. Root의 완료 진단/Planner progress/HTTP 범위34 passed, worker의 관련 완료·진단 통합33 passed이며 서로 겹친다. 전체 Ruff 및 캐시 없는 Linux 대상 source+tests mypy912파일이 통과했다. 실제 모델 성능 개선은 후속 라이브에서 확인해야 한다.

새 Argon 읽기 전용 진단은7분14초 후 완료됐으며 보고서는 `data/argon-loop-review-20261016/raw-review.md`에 보존했다. `Finding=0`을 일반적인 목표 완료 거절 규칙으로 추가하자는 권고는 기각한다. 목표에는 음성 결과나 조사 범위 검증도 있을 수 있으며 기존 원본·의미·정리 완료 게이트를 유지한다. `execution_started`는 대상 HTTP 송신 여부가 아니라 gateway가 provider 호출에 진입했는지를 뜻하므로 adapter 반환값을 그대로 우선하자는 제안도 실제 계약으로 검토한다. 출력이 생략되거나 schema 검증에서 거절된 결과의 자원 효과를 근거 없이 성공 선언으로 복구하지 않는다.

Gateway 독립 감사는 진입 직전 callback과 core 계약을 확인해 `execution_started` 덮어쓰기 결함 주장을 기각했다. 출력 검증 실패 경로에서 adapter metadata가 빠지는 것은 사실이나, 실제로 필요한 소비자나 안전한 복구 계약이 입증되지 않아 변경하지 않았다. 별도 읽기 전용 원장 감사는255개 정산 응답에서 공개 도구 호출160건 중 graph 관련30건(정확한 `graph.record`4건 포함)을 확인했다. 따라서 graph 호출0건을 근거로 initial exposure를 바꾸자는 원인 설명은 성립하지 않는다. 호출 이름의 존재와 handler 수락·최종 Finding 생성은 구분하며 이 Run의 Finding은 여전히0이다.

완료 거절 진단은 Luna 독립 검토에서도 추가 결함 없이 `0fe740e3`에 커밋했다. `CompletionIssues.add`의 메시지/typed 진단 대응, 미형식 오류 개수만 노출, 새 wake 후 과거 재계획 소진 판정 무효화, 자동 재시도 없음이 검토됐다. Root는 실제 checkout의 `.venv/Scripts/python.exe`로 새184824 fixture의 Docker/config·provider-reference 사전점검을 다시 실행해 둘 다 통과했다. 다른 워커에서 해당 인터프리터가 보이지 않았던 관측은 별도 환경 차이로 분리하며 그 워커의 새 preflight 성공으로 세지 않는다.

## 수정본 실제 재검증 — Run04c51

`7aeb2d40`을 정상 push하고 동결한561개 runtime 파일로184824 fixture를 실행했다. 첫 시도는 root가 prepared-only 표시를 너무 일찍 바꿔 CLI 전에 거절됐다. Run/모델/대상 호출이 없고 lock/프로세스가 없음을 확인한 뒤 실패 로그를 보존했다. 상태를 바로잡은 실제 실행은20:06:48 KST에 시작한 `run_04c51ceb92fc`다. 기존30분/300요청/3워커/모델 동시1·같은 prompt/승인·증명을 유지했다.

20:19:28 KST에 ReportProjected와 RunFinished가 기록됐다. 결과는 `failed / planning_error / unresolved`, 최종 진단은 hypothesis_planning의 `decision_repair_budget_exhausted`다. mission active/release false/Finding0이다.119개 모델 요청 모두 정산됐고 알려진 총 토큰3,520,015, Task6개는 confirmed1/inconclusive4/blocked1이다. 실행15건은 성공11/실패4이며 실패는 recon timeout이다. Kali HTTP 영수증8개에 Alice/Bob 계정 요청의 실제 세션 일치 원본2개가 포함된다. 계정 요청 둘 다200이지만 이 사실만으로 Finding을 만들지 않는다.

이번에는 현재 후보 revision2와 purpose가 일치하는 public/final security-check receipt가 생겼다. verdict/outcome은 unknown이고 후보는 open으로 남았다. 검토 선택→원본 검토 경로가 실제로 연결된 관측이며, 충분한 입증이나 모델 성능 목표 달성은 아니다. 세션 목록 원본2건에는 공급자의 빈 resource_effects가 보존됐다. Task/저장 결과6개 모두v2이고 `result_v1_extensions_forbidden` 재발0이다. 다만 버전을 생략하고 확장 필드를 넣은 실제 게시가 저장됐다는 사례는 이 Run에서 입증되지 않았다.

증거 JSONL/index241개와 DB 증거 참조64개의 무결성 문제0, 승인 대기/미전달 사건0, 소유 프로세스4개 종료를 확인했다. launcher는 완료됐지만 exit code는 unknown이다. Root는 동결 해제 전에561개 runtime hash 변경0을 확인했다. 독립 observer의 후속 hash 검사는 동결 해제 이후 수정된 파일을 별도로 기록한다. 안전한 최종 근거는184824 fixture의 `safeobserver-report.json`이다. 과거 Run을 새 코드·예산으로 재개하지 않는다.

### 마지막 거절 재현과 보정 여유 상향

최종 판단은 정상 요청12개 중1개, 보정6개 중6개를 사용했고 transport 실패는 없다. 마지막7회 중6회는 내용 없이 version만 있는 context_request였으며 `planner_lookup_needs_exactly_one_query`를 반복했다. 마지막1회는 조회를 null로 고친 뒤 question principal이 후보 범위를 벗어나 거절됐다. 유효한 제안이 버려진 근거는 없다. 저장된 요청 preview가 잘려 실제 제공자 schema 전체를 복구할 수는 없지만, 동결 commit의 생성 schema와 실제 parser 간 불일치는 재현됐다.

`c8b80031`은 조회 소유 모델의 JSON schema에 v1 단일 query/v2 1~4개 batch의 기존 조건을 표현한다. unused lookup은 null/생략, version-only/혼합 query는 거절한다. parser·조회 권한·저장 의미는 유지하며 Planner request 계약6에 결속한다. Root는 실제 실패 입력6개를 메모리에서 재검사했다. 동결 commit의 생성 schema는6/6을 허용했고 수정 schema는6/6을 거절했다. 이 재현은 형식 계약 검증이며 실제 모델이 다음에 올바르게 출력한다는 성능 보장은 아니다.

사용자는 이후 예산을 다른 에이전트와 비교해 여유 있게 맞추도록 명시적으로 요청했다. Strix pin의500 SDK turns/run과32 tool calls/turn, ARTEX의 agent turn 무제한+run1200초, PentestGPT pin의20 accepted Supervisor decisions와 task별 provider turns는 단위가 다르다. 같은 숫자나 동일 성능을 주장하지 않고 기존 원장/설정으로 유한한 여유를 늘렸다.

| 범위 | 이전 | 새 값 |
| --- | --- | --- |
| 판단 normal / repair | 12 / 6 | 24 / 12 (총36, transport 최대5는 이 총량 안에서 소비) |
| native/legacy Task 모델 요청 | 16 / 8 | 둘 다64 |
| Task 대상 실행 / 보정 | 4 / 6 | 16 / 16 (보정은64에 포함) |
| 연속 provider 실패 | 6 | 6 |
| 새 검증 fixture 전체 요청 / 시간 | 300 / 1800초 | 600 / 3600초 |

`aefe9749`는 config/settings와 기존 env 경로에 새 기본값을 연결하고 명시적인 과거12/6 직렬화·재개 identity를 유지한다. `374a628b`는 prompt를 바꾸지 않고 native28/legacy21 프로필에 Task 한도를 둔다. 과거 저장 ProbeLimits의 기본값을 바꾸지 않는다. 전체 Control/승인·증명, provider transport/실제 요청 원장,900초 판단/180초 시도 clock은 유지한다. 새 fixture203407은 별도 저장소·명시적인600/3600·24/12 설정으로 준비하며 이전 Run을 재개하지 않는다.

설정/원장 집중95 passed, profile21 passed, lookup schema·진단41 passed를 확인했다. Root의 Planner/atomizer/candidate/lookup 통합31개 중30개가 통과한 뒤 JSON Schema가 선택하는 오류 leaf에 의존하던1개 assertion을 수정해 해당1개도 통과했다. 경계가 달라진 admission fixture는 실제 profile 상한+1을 사용하고, 후보 fixture는 다른 조건인 status를 명시해 계약 버전 거절만 검사한다. 보정 반복 입력 수는 실제 설정에서 얻는다. 단일 영어 단어를 요구하던 오래된 설명 assertion도 현재 context_request 계약에 맞췄다. 전체 Ruff와 캐시 없는 Linux 대상 source+tests mypy913파일이 통과했다. 독립 Luna 소스 리뷰에서 새 계약/예산 결함은 없었으며, 지적된 기존 fixture와 문서 불일치를 반영했다. 개별 테스트 범위는 겹치며 전체 pytest 통과로 표현하지 않는다.
