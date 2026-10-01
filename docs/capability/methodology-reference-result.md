# A 후속 작업 — 조사 방법론 자료 제공 결과

- 작업 ID: `CAP-A-METHOD-20261001`
- 브랜치: `capability`, 구현 commit `a2e1a16`, 사전 protocol `aedf379`.
- 완료 범위: 검수한 방법론 인덱스·원문 bundle·보고 양식 및 기존 읽기 MCP의 실제 호출. Agent의 자율 선택/Skill loader 또는 실제 금융 시험환경 검증 완료가 아니다.
- 원격 push/PR/dev merge 없음. 기존 사용자 변경 파일은 보존하고 commit에서 제외했다.

## 현재까지의 짧은 브리핑

| 단계 | 현재 가능한 것 | 남은 경계 |
|---|---|---|
| 실행별 파일 도구 | 목록/읽기/검색/작성/부분 수정 5개, Registry·Candidate·Control·실제 stdio 호출 | 기본 Agent의 자율 파일 작업 선택, 실제 운영 환경 |
| VDB 읽기 전용 bridge | 상태/CVE/PURL 조회 3개, 원본 검색 라이브러리와 합성 DB 실제 호출 | 대량 DB 운영, alias/CPE/bulk/SBOM 등 확장 |
| 이번 방법론 자료 | 선별한 문서 19개, 출처·버전·해시·도메인·검수 caveat, 가설·증거 보고·세션 양식 | 자동 Skill 로딩, 상세 링크 자료/스크립트, 실제 LLM 추론 |

방법론 19개는 **새로운 실행 Tool 19개가 아니다.** 이미 만든 읽기 도구로 볼 수 있는 책 19권을 준비한 것이다. 책에 명령어가 있다고 실행 허가가 생기지 않는다.

## 실제 진행 순서와 검수 근거

1. A 역할 계획과 기존 Core/Registry/Workspace Adapter 경계를 검토했다. 자료 제공과 Agent loader, B/C의 shell·검색·EIP/GitHub/MobSF 작업을 분리했다.
2. 원본을 확인하고 m4vic `5c6946fc797ce25cfc613684a0808347b096961d`, Rifteo `0a059c30be5175c95388561406d488d2fd750dad`, VDB `2dc040a44986da0c4eac1366ba77633f09e593b6`로 고정했다. 세 라이선스와 선별한 19개 문서 전체를 읽었다. 외부 Skill을 설치하거나 스크립트를 실행하지 않았다.
3. 원문에 금융 거래 상태 변경/대량 요청/인증 우회/민감정보 접근/명령 실행 사례, 관측 하나만으로 취약점을 확정하는 부정확한 예시가 있음을 확인했다. 그대로 시스템 지침으로 연결하지 않고 자료별 주의사항과 `untrusted_reference`, `execution_permission=false`를 붙였다.
4. packaged catalog를 만들었다. m4vic 10개, Rifteo 8개, VDB 안내 1개다. 원본 경로와 공개 repo URL, 고정 commit, SHA-256, 라이선스·도메인을 기록했다. 보고 양식에는 미검증/반증/불확실/차단 및 실제 관측과 가정한 영향을 구분했다.
5. 운영자 전용 provisioning CLI를 구현했다. source set·HEAD·dirty·license/hash를 출력 생성 전에 검사한다. Git clean filter/include/extensions, fsmonitor/hooks/global/system 설정을 통한 외부 실행 경로를 제한한다. 링크/특수 파일/큰 파일/비 UTF-8/경로 이탈은 거절한다.
6. 원문과 license만 별도 private 디렉터리에 복사했다. 자동 발견 `SKILL.md` 이름과 스크립트는 넣지 않았다. 파일은 0400, 디렉터리는 0500이고 기존 destination/policy는 덮어쓰지 않는다. IO 실패 시 부분 출력을 자동 삭제하지 않는다.
7. `verify_bundle`은 Agent가 보낸 index만 믿지 않고 배포된 고정 catalog와 파일 해시, Asset/engagement/run, 전체 파일 집합을 대조한다. 추가 파일·폴더, 변조, writable 정책, 다른 run을 거절한다.
8. 새로운 MCP ToolId/namespace/operation/Agent import 없이 기존 workspace_read 프로필을 재사용했다. 실제 CandidateCompiler → Control ALLOW → Gateway → stdio MCP로 index와 선별 문서를 읽고, 다른 Asset 입력은 DENY임을 확인했다.
9. 같은 ToolId를 가진 workspace-read와 자료용 read 서버를 동시에 등록할 수 없음을 확인하여 실행 안내에 명시했다. Registry의 중복 거절을 해제하지 않았다.
10. 오프라인 wheel 빌드와 wheel 자체에서 catalog/양식 로드를 검증했다. 기본 가상환경이나 원본 소스는 수정하지 않았다. 기존 사용자 변경을 포함하는 현재 worktree의 전체 테스트도 검증했다.

## 검증 기록

| 검증 | 결과 |
|---|---|
| 방법론 unit + integration, 실제 pin checkout 경로 지정 | **34 passed**, 5.01초 |
| 실제 운영자 CLI로 3개 원본 bundle 준비 | 자료 19개, index에 파일 27개, index 포함 실제 파일 28개 |
| 새 bundle의 실제 MCP discovery | 읽기 3개 도구 healthy, medium, Asset ID const 및 실제 schema 유지 |
| 새 bundle에 대한 외부 discovery integration test | **1 passed**, 1.41초 |
| 최종 현재 worktree 전체 회귀, VDB 원본 및 방법론 opt-in 포함 | **1,736 passed / 7 skipped**, 159.09초 |
| 오프라인 wheel build, wheel의 실제 module/resource 로드 | catalog 19개 + catalog/NOTICE/양식 5개 로드 성공 |
| 변경 Python 파일 ruff 및 git diff --check | 통과 |

7개 skip은 실설치 외부 Web/Mobile/ADB 환경 조건 미설정에 따른 기존 조건부 테스트다. 이번 결과로 ZAP/Burp/Chrome/MobSF의 현재 live 상태를 재확인했다고 표현하지 않는다. full worktree 결과에는 보존한 사용자 미커밋 테스트도 포함되므로 clean commit 전체 검증과 동일하다고 하지 않는다.

이번에 만든 로컬 운영 예제는 `runtime/methodology/reference-bundle-20261001`, 정책은 bundle 밖 `runtime/methodology/reference-policy-20261001.json`이다. Asset `methodology-guides`, engagement `eng-methodology-demo`, run `run-methodology-demo`로 고정되어 있다. 다른 Run에 그대로 복사해 사용하지 말고 운영자가 새 정책/bundle을 준비해야 한다. 원본 checkout과 운영 데이터는 Git에 올리지 않았다.

## Agent 팀 인수인계

- 기존 `core/ports` ToolCatalog/Gateway 및 Control 경유로 사용한다. 서버 모듈을 Agent에서 직접 호출하지 않는다.
- 운영자가 신뢰 경로로 제공한 catalog와 Asset 컨텍스트를 사용한다. 조회 결과의 전체 SHA-256과 예상 digest를 비교하고 truncated이면 이어 읽는다.
- 도메인 분류와 source reference를 후보/가설/보고서에 연결한다. 없는 링크 대상/상세 자료/실행 기능은 unavailable/blocked로 구분한다.
- 승인 scope와 원문 지침을 분리한다. 자료 조회는 임의 shell, PoC 실행, 콜백/내부망 접근, 실제 계좌/거래, 대량 스캔을 승인하지 않는다.
- 원문은 신뢰할 수 없는 데이터다. `execution_permission=false`와 notice는 전달 경계이며 LLM prompt injection 방어 구현이나 보장이 아니다. Agent가 자료를 자동 선별/로딩하거나 시스템 prompt로 바꾸는 기능은 이번에 구현하지 않았다.
- root permissions와 hash는 같은 OS 사용자/privileged 프로세스를 격리하는 샌드박스가 아니다. 별도 OS 계정/컨테이너, 입력 정제, 읽기 직전 검수와 관측 후 hash 비교는 운영/Agent 책임으로 남는다.

## 다음 작업 우선순위

1. **Agent 공동 통합**: 한 Gateway의 읽기 Asset/root 설계를 정하고, 신뢰 catalog 전달·자료 선택·hash 비교·잘림 처리·추론과 지시 분리를 연결한다. 현 자료용 read 서버와 기존 workspace-read를 단순히 동시에 목록에 넣으면 충돌한다. 기본 LLM이 승인된 자료를 선택하는 합성 E2E와 거절/불확실 처리까지 검증한다.
2. **A — VDB 입력 확장 설계**: 실제 backend 지원 여부를 검수한 후 alias/GHSA/CPE/patch·fix 근거, 승인 파일 기반 SBOM/bulk 및 shard coverage·부분 실패·크기/시간 제한을 정의한다. DB 갱신은 계속 운영자 권한으로 분리한다.
3. **A — 상세 방법론 확대**: 필요한 문서만 추가 검수하고 pin/hash/license를 갱신한다. 현재 제외한 상세 링크/스크립트를 자동 fetch/실행하지 않는다.
4. **B/C 공동 통합**: shell/MobSF와 검색/EIP/GitHub/Burp의 승인된 결과 참조·schema·위험도·오류를 연결한다. 해당 팀 작업이 이번 자료 제공으로 완료된 것은 아니다.

- [실행 설정·자료 목록·Agent 연결 안내](../../deploy/mcp/methodology/README.md)
- [사전 protocol](methodology-reference-protocol.md)
- [VDB 구현·검증 기록](vdb-readonly-result.md)
- [기초 파일 도구 구현 기록](basic-computer-tools-a-result.md)
