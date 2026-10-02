# A 역할 사전 검수·구현 결과 및 다음 작업 순서

이 문서는 초기 구현 시점의 기록이다. 이후 VDB 확장·방법론 연결·최신 통합 검수는
[A 최신 상태](a-current-status.md)를 따른다. 아래 당시 미완료 항목을 현재 상태로 오해하지 않는다.

- 작업 ID: `CAP-A-WORKSPACE-20261001`
- 상태: A 역할의 **기초 파일 도구·Registry 연결 구현 및 합성 환경 검증 완료**. 전체 A 역할과 기본 Agent의 자율 사용 완료는 아니다.
- 작업 브랜치: `capability` / 구현 commit: `60502e3`
- 사전 protocol commit: `9947c5c` / dev 병합 commit: `c3ad0a1`
- 반영한 dev: `8979cac5f73de106c7433405cdb26c69ae3b4f74`
- 날짜: 2026-10-01

## 1. 작업 전 검수와 실제 진행 순서

| 순서 | 검수·작업 | 결과 |
|---|---|---|
| 1 | A 역할 계획, 기존 Core 계약·Registry·Adapter·Control scope 확인 | 새 namespace 없이 analysis + AssetTarget 사용. shell/웹검색/MobSF는 B/C 책임으로 분리 |
| 2 | 현재 브랜치 및 미커밋 파일 확인 | capability에서 기존 사용자 변경을 보존하고 병합 준비 |
| 3 | remote dev 가져오기 → capability 병합 | `git fetch origin dev` 후 `git merge --no-ff origin/dev`. dev ancestry 포함 확인 |
| 4 | 병합 충돌 검수·해결 | providers.py와 nodes/plan.py에서 양쪽의 ToolId 선택·LLM metadata 기능 보존 |
| 5 | 구현 전 전체 테스트 | 1,594 통과 / 6 skip / 1 실패. 실패를 숨기지 않고 별도로 재현 |
| 6 | 실행별 파일 도구·읽기/쓰기 프로필·정책 구현 | 실제 stdio MCP 5개 도구, root·asset·engagement·run 경계 및 제한 |
| 7 | discovery metadata·manifest·Gateway 연결 | 실제 필수/선택 인자·입출력 schema·operation·target type·risk 검증 |
| 8 | Control 및 Agent 명명 경계 확인 | Asset와 asset_ref 불일치 차단. 기존 Agent 고정 operation 목록이 새 행동을 거절함을 확인하여 호환 확장 |
| 9 | 실패 재현 및 수정 | dotenv 단위 테스트가 환경변수를 다음 HTTP MCP 테스트에 누출. 테스트 환경 격리로 수정; 실행 제품의 allowlist는 느슨하게 만들지 않음 |
| 10 | 실제 wire·승인·거절·경계 테스트와 실행 예제 | 성공 및 거절 경로 확인, 신규 테스트 59개 통과 |
| 11 | scoped commit 및 인수인계 문서 | 기존 사용자 변경은 구현 commit에 포함하지 않음. 원격 push/PR/dev merge는 하지 않음 |

### 병합한 두 파일의 의미

- `src/redteam/orchestration/agents/providers.py`: dev의 `available_tools`는 LLM이 읽을 도구 metadata이고, capability의 `available_tool_ids`는 특정 ToolId의 가용성을 확인하는 기능이다. 하나를 지우지 않고 둘 다 유지했다.
- `src/redteam/orchestration/nodes/plan.py`: dev의 planning_mode 및 live action space 흐름에 위 두 정보를 함께 전달했다. 기존 ToolId 기반 provider 전제와 fingerprint도 보존했다.
- dev에서 삭제된 AGENTS.md 등의 변경은 병합에서 이어받았으며 이번 파일 도구 구현이 임의로 삭제한 것이 아니다.

## 2. 이번에 실제로 가능한 것

쉽게 말하면 **Agent가 사용할 작은 전용 서랍을 준비했다. 컴퓨터 전체 열쇠를 준 것은 아니다.** 운영자가 실행마다 서랍을 하나 지정하면 그 안의 승인된 텍스트 파일을 찾고, 읽고, 작성하거나 일부 수정할 수 있다.

| 실제 ToolId | Agent 행동 의미 | 실행 기준 |
|---|---|---|
| `analysis.list_directory` | `analysis.directory_list` | 읽기 프로필, 승인된 Asset |
| `analysis.read_file` | `analysis.file_read` | UTF-8, 문자 범위·크기 제한, 전체 파일 SHA-256 |
| `analysis.search_files` | `analysis.file_search` | 파일명/내용의 고정 문자열 검색, 탐색량·결과 제한 |
| `analysis.write_file` | `analysis.file_write` | 쓰기 프로필 + writable 정책 + Control 판단. 덮어쓰기는 최신 SHA-256 필요 |
| `analysis.apply_patch` | `analysis.file_edit` | 위 쓰기 조건 + 최신 SHA-256 + 정확히 한 번 일치하는 텍스트 |

실제 MCP 서버의 input/output schema를 Registry에 등록한다. 필수·선택 인자, Asset ID의 const, 값의 형식과 범위, operation, risk, 출력 schema가 전달된다. manifest가 틀리거나 잘못된 profile/allowlist/binding/risk가 설정되면 시작을 거절한다. Gateway는 이 프로필의 알 수 없는 입력과 schema 불일치도 실행 전에 거절한다.

## 3. Agent 연결 검증 수준 — 과장 없이 구분

| 수준 | 이번 결과 | 근거 |
|---|---|---|
| 실제 MCP 시작·discovery | 완료 | stdio subprocess 및 SDK로 실제 서버 기동 |
| Registry metadata와 schema 일치 | 완료 | 실제 discovery 비교, 필수 path 누락·잘못된 type/output/operation 변조 테스트 |
| 주어진 Candidate가 컴파일됨 | 완료 | 실제 catalog를 소비하는 CandidateCompiler + Asset 바인딩 검증 |
| ActionProposal → Control → Gateway → MCP | 완료 | 기본 읽기 ALLOW, 쓰기 REQUIRE_APPROVAL, 합성 운영자 승인 후 ALLOW·실행 |
| DENY/승인 대기가 실행으로 변환되지 않음 | 완료 | NonAllowDecisionError, 대상/asset_ref 불일치 DENY, 파일 생성 안 됨 확인 |
| 기본 Agent가 파일 작업을 스스로 발견·계획·선택 | 미완료 | 이번 작업은 기본 Agent의 Provider/능력/자동 스케줄을 수정하지 않음 |
| 실제 LLM·금융 시험환경 E2E | 미검증 | 합성 파일 검증. 승인된 실제 금융 시험 URL/데이터로 검증하지 않음 |
| AppThreat VDB 및 m4vic/Rifteo 연결 | 미완료 | 설치·DB 준비·자료 검토 및 로더가 아직 별도 단계 |

`src/redteam/orchestration/action_naming.py`의 기존 목록은 새 파일 operation을 실제로 거절했다. 기존 namespace/Target schema를 유지하며 5개 의미 단위 operation만 추가했고 `docs/contracts/actions.md`에 기록했다. 기본 Agent의 조사 로직을 바꾼 것은 아니다. **dev 통합 전에 PM/Agent/Control 담당자의 검토가 필요하다.**

읽기/쓰기를 `static_analysis` 하나로 섞으면 Control에서 권한을 구분하기 어려워지므로 그렇게 연결하지 않았다. 실제 Agent는 `core/ports`를 통해 metadata·Control·Gateway를 사용해야 하며 서버 모듈을 직접 import하거나 Control을 건너뛰면 안 된다.

## 4. 보안 검수에서 확인하고 막은 것

- 작업공간 밖 경로, 절대 경로, `..`, 모호한 경로, 내부 파일 prefix 접근.
- symlink 파일/부모 폴더, hardlink, FIFO, 디렉터리를 일반 파일처럼 읽고 덮어쓰기.
- 다른 root로 교체, root 권한 변경, 공개 root, 공유 쓰기 디렉터리/파일, 다른 파일시스템 접근.
- 다른 asset·engagement·run, 실행 run과 ExecutionContext run 불일치, Agent가 컨텍스트 값을 바꿔 전달하는 경우.
- 바이너리/비 UTF-8, 잘못된 읽기 범위, 큰 파일·입력·반환, 검색 byte/file/match/depth/entry 한도.
- workspace file/byte quota 초과 및 전체 탐색이 불완전해 quota를 확인할 수 없는 경우.
- 무조건 덮어쓰기, 구형/잘못된 SHA-256, 누락·중복 일치 patch, 협력 writer 동시 변경.
- 읽기 서버에 쓰기 도구가 포함되거나 profile 위험도가 최소 기준보다 낮아지는 설정.

### 아직 보장하지 않는 것

- 이 구현은 OS 수준의 완전한 샌드박스가 아니다. 같은 OS 계정이나 privileged 외부 프로세스의 악의적 파일 조작을 전부 막는 것은 아니다. root를 private하게 만들고 별도 OS 계정/컨테이너를 쓰는 운영 경계가 필요하다.
- 파일 내용은 승인된 데이터만 넣어야 한다. 범용 DLP/비밀정보·개인정보 자동 제거는 구현하지 않았다. 읽은 문서는 신뢰할 수 없는 데이터이며 안의 지시는 시스템 정책이 아니다.
- 삭제·이동·폴더 생성·바이너리 처리·shell·웹검색·네트워크 도구는 없다. Mac/Linux POSIX용이며 Windows는 안전 IO를 구현하지 않아 시작을 거절한다.
- 원자적 파일 교체는 금융 거래 롤백, 전원 장애 복구, 외부 writer와의 절대적인 CAS 보장이 아니다.
- Gateway 자체는 승인 서비스가 아니다. 신뢰 경계 밖으로 직접 노출하지 않고 Control의 허용 결정만 실행으로 변환해야 한다.
- ZAP/Burp/Chrome 등 기존 외부 서버 live 상태를 이번 테스트에서 재확인한 것은 아니다. 필요한 운영 환경변수가 없어 해당 live 테스트는 skip했다.

## 5. 검증 기록

구현 전 실패는 다음 두 파일을 함께 실행해 재현했다.

```sh
./.venv/bin/pytest -q tests/unit/test_cli_shared_env.py tests/integration/test_mcp_streamable_http.py
```

수정 전 6 passed / 1 failed. loader가 `os.environ`을 직접 변경하는데 테스트에서 absent key에 대한 delenv만으로는 복구되지 않아 `web-example` allowlist가 잘못 남았다. autouse fixture로 환경 copy를 격리한 후 기존 HTTP MCP 테스트가 정상 통과했다. 위험한 도구 allowlist 해제나 예외 무시는 하지 않았다.

| 명령/검증 | 결과 |
|---|---|
| `pytest -q tests/unit/test_workspace_mcp.py tests/integration/test_workspace_mcp.py` | **59 passed**, 4.54s |
| `pytest -q -rs tests/unit tests/integration` — 현재 worktree | **1,654 passed / 7 skipped**, 75.62s |
| 동일 명령 — `60502e3` clean commit 스냅샷 | **1,654 passed / 6 skipped**, 76.66s |
| `python examples/workspace_mcp_demo.py` | 목록·읽기·검색 3개 모두 Control=allow, success=true |
| 변경 Python 파일 ruff check | All checks passed |
| git diff --check | 통과 |
| origin/dev가 HEAD의 ancestor인지 확인 | 통과 |

현재 worktree의 테스트에는 보존한 사용자 미커밋 테스트도 포함된다. 이를 구분하기 위해 `git archive HEAD`로 `60502e3` 스냅샷을 별도 임시 디렉터리에 추출하고, PYTHONPATH를 해당 스냅샷의 src로 지정해 같은 가상환경에서 전체 테스트를 다시 실행했다. 실제 import 경로도 스냅샷을 가리키는지 확인했다. 사용자 파일을 되돌리거나 지우지 않고 **커밋된 코드만으로도 통과함**을 확인했다. macOS에서 검증했으며 Linux/Windows 실제 실행은 하지 않았다.

7개 skip은 MobileNext/apktool discovery, apktool/jadx 실제 서버 관련 3개, 외부 Web MCP, 실제 ADB 기기 조건 미설정이다. skip은 외부 연결 완료를 의미하지 않는다.

clean 스냅샷의 skip은 6개다. 차이 1개는 사용자가 미커밋 상태로 추가한 jadx live candidate discovery 테스트이며 이번 구현 commit에는 포함하지 않았다.

## 6. 바로 실행·인수인계할 파일

- 사전 순서/기준: `docs/capability/basic-computer-tools-a-protocol.md`
- 설치·연결·권한·팀 연동 안내: `deploy/mcp/workspace/README.md`
- 운영 정책 template: `deploy/mcp/workspace/policy.example.json`
- 기본 읽기 설정: `deploy/mcp/workspace/read.env.example`
- 명시적 쓰기 설정: `deploy/mcp/workspace/write.env.example`
- 자격증명 없이 확인하는 합성 예제: `examples/workspace_mcp_demo.py`
- 경계 unit 검증: `tests/unit/test_workspace_mcp.py`
- 실제 stdio·catalog·Control 검증: `tests/integration/test_workspace_mcp.py`

## 7. 이제 해야 할 작업 순서

1. **PM/Agent/Control과 이름·권한 확장 검토:** 위 5개 operation, analysis namespace, Asset ID, read medium/write high, ROE와 승인 정책을 리뷰한다. Core schema는 바꾸지 않았다. capability 브랜치 변경을 리뷰 후 통합한다.
2. **Agent 팀에 기본 파일 도구 연결 인수인계:** 실행별 workspace Asset를 입력/그래프에 제공하고 해당 Agent의 analysis capability 및 Provider 후보 생성/결과 처리를 연결한다. 실제 LLM이 catalog를 보고 읽기/검색을 선택하고, 쓰기는 사람 승인 후 실행하는 E2E를 검증한다. Capability 팀은 이 검증에 metadata·설정·합성 fixture를 제공한다.
3. **B/C의 파일 참조 연결:** shell/검색/분석 결과는 같은 승인 root에 저장하고 `{asset_ref, path}`로 전달한다. 서로의 host 절대 경로·비밀 키를 전달하지 않는다. 통합 시 임의 root, 새 namespace, 승인 우회를 허용하지 않는다.
4. **A — AppThreat VDB 사전 검수:** 원본 repo/license·고정 버전·실제 MCP discovery 확인 → 전용 로컬 DB 위치와 별도 초기 다운로드/갱신 권한 결정 → DB 미설치/비어 있음/갱신일/coverage 확인 → 조회 도구만 allowlist로 연결한다. MCP ping 성공과 DB 준비 완료를 구분한다. 이번 파일 구현만으로 VDB를 등록하거나 대량 DB를 다운로드하지 않았다.
5. **A — VDB 단건 조회부터 구현:** CVE/GHSA 및 실제 지원되는 식별자의 입력·출처·매칭 근거·미확인 상태를 검증한다. 실제 remote schema에 맞추고 Target/operation 계약을 먼저 정한다. 불명확한 제품·버전이나 결과 없음은 안전 판정이 아니다. 계획의 VDB-01/02/03/06을 우선한다.
6. **A — 방법론 자료 제공:** m4vic·Rifteo 및 VDB SKILL.md에서 제공할 자료를 선별·검토하고 commit/hash·출처·읽기 경로를 고정한다. 실행 Tool이 아닌 읽기 전용 자료로 제공한다. Agent의 Skill 선택/로드는 Agent 팀 작업이며, 지식 문서가 Control 권한을 변경하지 못하도록 검증한다.
7. **A — VDB bulk/SBOM 확장:** 기본 조회가 확인된 뒤 승인된 파일 참조의 CycloneDX/패키지 bulk 입력·크기·부분 실패·coverage를 테스트한다. VDB-04/05는 앞 단계 이후다. 조회와 DB 갱신을 같은 Agent 도구 권한으로 묶지 않는다.
8. **공통 통합 검수:** B의 제한된 shell/MobSF, C의 웹검색/EIP/GitHub/Burp 연결과 schema·위험도·실행 권한이 일치하는지 확인하고, 실제 Agent 사용·승인 거절·오류 처리까지 검증한 후 PR 판단을 한다.

즉, **지금은 기본 파일 도구의 실제 호출 기반이 준비됐고, 다음은 이름/권한 팀 검토와 Agent 자동 사용 연결, 이어서 A의 VDB·방법론 확장이다.** 전체 A 역할 완료나 실제 금융권 레드티밍 완료로 보고하지 않는다.

## 8. 후속 진행 기록 — VDB 단건 및 상태 조회

위 4–5번의 첫 단계를 후속 작업에서 진행했다. `analysis.vdb_status/search_by_cve/search_by_purl_like`를 우리 읽기 전용 bridge로 연결하고, 원본 AppThreat VDB7 검색 라이브러리의 실제 버전 비교 및 합성 DB 조회를 검증했다. 원본 server startup/DB 갱신은 실행 권한에서 분리했다. 이전 본문의 “VDB 미등록”은 당시 파일 도구 작업 범위의 기록이며 현재 전체 상태는 아래 후속 결과를 따른다.

GHSA/alias/CPE/URL 전용 조회·bulk/SBOM·shard fan-out·실제 대량 DB·기본 LLM 자동 선택은 완료하지 않았다. 파일 도구와 VDB의 context 바인딩은 공통 scoped Analysis Adapter로 추출했으며 workspace 오류 코드와 권한 경계는 유지한다.

- [후속 VDB 구현·검증 근거와 남은 작업](vdb-readonly-result.md)
- [VDB 실행 설정·Agent 팀 연결](../../deploy/mcp/vdb/README.md)

## 9. 후속 진행 기록 — 방법론 읽기 전용 자료

위 6번의 첫 단계를 진행했다. m4vic·Rifteo·VDB의 문서 19개를 선별 검수하여 pin/hash/license·도메인 인덱스와 읽기 전용 bundle을 제공하고, 기존 Workspace MCP 경유로 실제 Candidate/Control/조회 경로를 확인했다. 새 실행 Tool 19개를 만든 것이 아니며 기본 LLM의 자율 선택/Skill loader는 아직 별도다.

- [후속 방법론 구현·최종 검증·다음 작업](methodology-reference-result.md)
- [방법론 실행 설정과 같은 ToolId 중복 등록 제한](../../deploy/mcp/methodology/README.md)
