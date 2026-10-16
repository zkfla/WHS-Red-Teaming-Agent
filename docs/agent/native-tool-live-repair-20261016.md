# 도구 선택·호출 연결 라이브 점검 — 2026-10-16

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
