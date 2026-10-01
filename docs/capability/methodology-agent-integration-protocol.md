# CAP-A-METHOD-AGENT-20261001 — opt-in 자료 조회 Agent 연결 protocol

- 요청: 직전 결과의 Agent 자료 선택·로딩 통합을 이어서 진행한다.
- 기준: capability `1f401f3`, 자료 구현 `a2e1a16`. 사용자 미커밋 변경 보존. 원격 push/PR/dev merge 없음.
- 외부 자료 추가 다운로드/Skill 설치/스크립트 실행/실제 금융 대상 요청 없음.

## 사전 검수 결과와 결정

- 같은 read ToolId를 두 서버에 등록할 수 없다. 단일 실행별 Workspace root 아래 검수 bundle을 두고 기존 read/write 쌍만 사용한다. Registry 중복 거절·namespace·ToolId·operation은 변경하지 않는다.
- 기존 Recon/Web/Mobile의 capabilities와 scheduling 정의에 analysis를 자동 추가하지 않는다. 기본 Analysis 역할은 검증용 seam이므로 이를 실행 Agent로 바꾸지 않는다.
- 명시적 config로만 별도의 자료 조회 Agent를 켠다. 선택 자료 ID와 paging 크기는 운영자가 제한한다. 자료 조회도 CandidateCompiler → Control → Gateway → MCP → Evidence를 따른다.
- 현재 run/engagement ID는 자동 생성된다. opt-in 구성에서만 운영자가 사전 준비한 정책 ID로 바인딩하고 명시적 ID 불일치는 거절한다. 기본 구성의 생성 흐름은 유지한다.
- 자료를 검증했다고 내용이 지시 권한을 얻지 않는다. 본문은 사용자 데이터 영역에만 전달하고 ROE/시스템 prompt/실행 정책으로 승격하지 않는다.

## 구현 순서와 종료 조건

1. WorkspacePolicy에 기본 빈 readonly_paths를 추가해 자료 하위 경로의 쓰기/patch를 명시적으로 차단한다. 기존 정책과 MCP schema는 호환 유지한다.
2. composition root에서만 검수 bundle 및 workspace policy의 경로·Asset·engagement·run·hash·선택 자료 ID를 확인한다. Agent는 Capability 서버 구현을 import하지 않고 metadata DTO와 core ports만 사용한다.
3. opt-in Reference Agent/provider를 등록한다. 고정된 자료 중 순서대로 선택하고 이어 읽는다. 새로운 target/권한을 추측하거나 scope에 자동 추가하지 않는다.
4. 실제 결과의 scope/path/offset/size/paging을 검사하고 Evidence에 제한된 chunk를 기록한다. 완전한 UTF-8 본문을 재조립한 후 SHA-256을 독립 계산한다. 서버가 hash를 말한 것만으로 검증 완료하지 않는다.
5. 불완전/변조/다른 Run/실패 결과를 계획 본문에서 제외한다. 검증한 자료만 도메인·문자 수 상한 안에서 기존 LLM candidate context에 참고 데이터로 전달한다.
6. 중복 read 서버 없이 일반 파일 조회와 자료 조회, 쓰기 보호, 실제 MCP/Agent loop/LLM 호출 seam, 거절·hash drift·paging·scope 실패를 테스트한다. 실제 외부 LLM 자율 성능과 합성 RecordingLLM 검증을 구분한다.
7. 실행 설정·예제·인수인계·제한을 기록하고 scoped local commit한다. 기능 미설정 시 기존 동작을 유지하는 전체 회귀를 확인한다.

config/RuntimeDeps의 선택적 확장은 Agent/PM 검토 대상이다. 기본 Agent의 도구 권한, Control 승인 및 관측 기반 Finding 판단은 확대하지 않는다.
