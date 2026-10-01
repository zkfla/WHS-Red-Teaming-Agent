# A 후속 결과 — 검수 자료의 Agent 조회·LLM 데이터 전달

- 날짜: 2026-10-01
- branch: `capability`
- 사전 protocol: `1f778a8`, 구현: `4d40c15`
- 원격 push / PR / dev merge 없음. 기존 사용자 미커밋 변경 5개는 보존하고 commit에서 제외했다.

## 결론

준비된 자료를 운영자가 최대 3개 지정하면 별도 `methodology-agent`가 **실제 기존 Workspace MCP**로 읽는다. 모든 페이지를 받은 뒤 UTF-8 본문의 SHA-256을 독립 계산한다. 검증에 성공한 관련 자료만 기존 LLM 후보 생성기의 user 데이터 영역에 전달한다.

12세 수준으로 설명하면: 책 목록에서 운영자가 읽어도 되는 책을 고른다 → 자료 담당 Agent가 페이지를 빠짐없이 읽는다 → 준비한 책과 같은 책인지 확인한다 → 다른 Agent에게 필요한 부분을 참고 자료로 건넨다. 책 속의 명령이 실행 허가가 되는 것은 아니다.

## 실제 작업 순서와 근거

1. 기본 Recon/Web/Mobile capabilities, Analysis verifier, provider/registry, App bootstrap, Evidence 기록과 planning 진행 판단을 검수했다. 기존 Agent에 analysis 권한을 일괄 추가하지 않기로 했다.
2. 중복 read ToolId 문제를 서버 하나의 **부모 Workspace + 자료 하위 폴더**로 해결했다. 기존 read 도구 3개와 선택적 write 도구 2개만 사용하며 Registry 중복 거절과 MCP Tool schema를 바꾸지 않았다.
3. WorkspacePolicy에 기본 빈 `readonly_paths`를 추가했다. 보호 경로 및 하위 경로, 그 부모에 대한 write/patch를 거절한다. OS mode가 writable이어도 적용된다.
4. App composition에서 policy 절대 경로, root/prefix, Asset/engagement/run 일치, 쓰기 보호, packaged catalog와 bundle 무결성, 선택 ID를 검사한다. Scope에 없는 Asset을 자동 승인하지 않는다.
5. `CapabilityConfig.methodology`가 있는 경우에만 별도 Reference Agent/provider를 등록했다. caller-owned Registry는 복사하고 기본 Agent와 Analysis verifier는 그대로 유지했다. 조회 Agent는 운영자가 지정한 문서만 deterministic 후보로 만들며 일반 LLM의 임의 파일 선택을 맡기지 않았다.
6. 실제 CandidateCompiler → Control → Gateway → stdio MCP 결과를 Evidence에 제한된 chunk로 기록했다. 잘린 문서는 offset/next_offset으로 이어 읽고 실패·다른 Run·경로/페이지 불일치·변조·충돌 중복을 제외한다. MCP가 반환한 hash를 말로만 믿지 않고 완전한 본문을 다시 hash한다.
7. 초기 통합에서 기존 진행 판단이 같은 Asset의 이어 읽기를 새 진행으로 보지 않아 중단됨을 확인했다. Graph에는 본문을 넣지 않고 자료 ID·검증 상태·다음 위치만 반영해 해결했다.
8. 관련 도메인의 검증된 본문만 문자 수 상한 내에서 기존 LLMCandidateProvider에 전달했다. 원문은 JSON user 데이터이고 시스템 prompt에는 자료 신뢰 경계를 설명하는 정적 규칙만 opt-in으로 추가한다. 기능이 꺼져 있으면 기존 시스템 prompt와 fingerprint 형식도 유지한다.
9. 설정을 일반 run/resume CLI loader의 명시적 `REDTEAM_METHODOLOGY_CONFIG_JSON`에 연결했다. AppConfig API/recording demo는 capability.methodology 객체를 사용한다. 운영자 policy의 ID로 바인딩하고 명시 ID 불일치 및 기존 Evidence가 있는 Run의 새 run 시작은 거절한다.
10. 실제 고정 원본 3개로 새 composite bundle을 준비하고 standalone demo까지 실행했다. 파일 도구 호출 2회로 m4vic-workflow와 rifteo-surface가 verified가 되었고, recording LLM 호출 2회 모두 참고 데이터가 전달됐다. 스캐너/브라우저/쓰기/대상 네트워크 도구는 demo에서 켜지 않았다.

## 검증 기록

| 검증 | 결과 |
|---|---|
| 자료 Agent/config/loop + 기존 LLM provider/mission 단위 회귀, 실제 pin 경로 지정 | **59 passed**, 5.29초 |
| 쓰기 MCP + Control REQUIRE_APPROVAL → 합성 운영자 승인 → 일반 파일 작성 | 성공 |
| 동일 승인 후 자료 파일 patch / 자료 폴더 신규 작성, OS mode도 writable | 실제 MCP에서 거절, 원문 보존 |
| 실제 m4vic/Rifteo standalone recording demo | Tool 호출 2회, 자료 2개 verified, LLM 입력 전달 2회 |
| 최종 현재 worktree 전체 회귀, VDB 및 방법론 실제 원본 opt-in | **1,782 passed / 9 skipped**, 89.28초 |
| 변경 Python ruff / git diff --check | 통과 |
| 오프라인 wheel build 및 wheel 자체에서 새 App/Agent module·catalog 로드 | 새 module 2개 + 자료 metadata 19개 로드 성공 |
| 예제 AppConfig/WorkspacePolicy 파싱 및 shell env 문법 검사 | 통과 |

전체 테스트에는 보존한 사용자 미커밋 테스트도 포함된다. clean commit만 따로 실행한 결과라고 표현하지 않는다. 9개 skip 중 7개는 apktool/jadx/외부 Web/ADB/Mobile 실설치 설정 부족이고, 2개는 sandbox의 로컬 TCP bind 제한(TLS·Streamable HTTP)이다. 이 결과로 ZAP/Burp/Chrome의 현재 live 상태까지 재확인한 것은 아니다.

이번 실제 demo의 새 운영 자료는 `runtime/methodology/agent-demo-YtF2py/` 아래에 있다. Workspace root와 bundle/별도 policy/AppConfig를 포함하며 Git에는 넣지 않았다. Asset `workspace-methodology`, engagement `eng-methodology-agent`, run `run-methodology-agent`에 묶였고 이미 Evidence가 있으므로 새 실행으로 재사용하지 않는다. memory checkpoint demo의 재개를 제공하지 않으며 새 테스트는 새 Run 정책/bundle로 준비한다.

## 현재 가능한 것 / 남은 것

- 가능: 명시 config로 선택 자료 최대 3개 조회, 실제 MCP paging, scope/쓰기 보호, 전체 body hash 검증, 관련 Agent의 LLM 입력 데이터 전달.
- 가능: 같은 read 서버로 일반 Workspace 파일도 조회. write 서버를 붙이면 일반 파일 쓰기는 기존 별도 사람 승인 흐름을 유지하고 자료 prefix는 계속 변경 불가.
- 아직 아님: 기본 Agent의 모든 파일 업무 자율 계획, LLM의 자유로운 Skill 선택, 외부 Skill 설치/스크립트 실행, PoC 자동 실행, 실제 금융 사이트 취약 확정.
- 아직 미검증: 실제 외부 LLM의 방법론 활용 품질/주입 저항성, 이번 opt-in의 durable 중단·재개 E2E, interactive wizard의 별도 config 연결.
- 본문은 신뢰할 수 없는 데이터다. hash/pin/notice/정적 prompt 규칙만으로 prompt injection 방어가 완성됐다고 하지 않는다. 기존 Compiler/Control/scope/도구 allowlist를 유지한다.
- 새 Core config/RuntimeDeps/Graph context/자료 Agent의 opt-in 확장은 dev 통합 전 PM·Agent·Control 공동 검토 대상이다.

## 다음 순서

1. Agent 팀과 optional config/Agent 등록·도메인 자료 전달을 공동 검토하고, 승인 대기·durable 재개와 실제 LLM 합성 평가를 별도로 검증한다.
2. A의 VDB 후속: GHSA/alias·영향/수정 근거·승인 파일 기반 SBOM/bulk의 backend 지원과 coverage/부분 실패/시간·크기 제한을 먼저 검수한다. DB 다운로드/갱신은 운영자 작업으로 계속 분리한다.
3. B/C의 shell/MobSF/검색/EIP/GitHub/Burp 결과는 승인 Workspace 참조를 통해 통합한다. 이번 자료 Agent로 다른 담당자 기능까지 완료된 것은 아니다.

- [실행 설정과 인수인계](../../deploy/mcp/methodology/README.md)
- [AppConfig 예제](../../examples/methodology-agent-app.yaml)
- [사전 protocol](methodology-agent-integration-protocol.md)
- [이전 자료 제공 단계](methodology-reference-result.md)
